from unittest.mock import patch

import anyio
import httpx
import pytest

from roomcast import server
from roomcast.media import MediaInput, MediaItem


class FakePlayer:
    def __init__(self, kind="Sonos", rooms=("Kitchen",), outcome=("Kitchen", 1, "started")):
        self.kind = kind
        self._rooms = list(rooms)
        self._outcome = outcome
        self.calls = []

    async def rooms(self):
        """The handle for a room is its name, which play() hands back as the room it used."""
        self.calls.append(("rooms",))
        return {name: name for name in self._rooms}

    async def play(self, room, items, volume):
        self.calls.append(("play", room, items, volume))
        return room

    async def append(self, room, items, after_current=False):
        self.calls.append(("append", room, items, after_current))
        return self._outcome

    async def queue(self, room):
        return f"queue of {room}"


@pytest.fixture(autouse=True)
def fresh_rooms():
    with patch.object(server, "_rooms", {}), patch.object(server, "_rooms_at", float("-inf")), \
            patch.object(server, "_known", {}):
        yield


def _players(*players):
    return patch.object(server, "PLAYERS", list(players))


def test_first_backend_owns_a_shared_name_and_kind_prefix_reaches_any_room():
    sonos = FakePlayer("Sonos", ["Kitchen", "Den"])
    cast = FakePlayer("Cast", ["kitchen", "Nest Hub"])
    with _players(sonos, cast), patch.object(server, "DEFAULT_ROOM", "Kitchen"):
        assert anyio.run(server.list_rooms) == (
            "cast:kitchen (Cast), Den (Sonos), Kitchen (Sonos), Nest Hub (Cast). Default: Kitchen.")
        assert anyio.run(server.resolve_room, None) == (sonos, "Kitchen")
        assert anyio.run(server.resolve_room, "nest hub") == (cast, "Nest Hub")
        assert anyio.run(server.resolve_room, "SONOS:kitchen") == (sonos, "Kitchen")
        assert anyio.run(server.resolve_room, "cast:Kitchen") == (cast, "kitchen")
        assert anyio.run(server.resolve_room, "sonos:Den") == (sonos, "Den")
        with pytest.raises(ValueError, match="No speaker named 'Garage'. Known: cast:kitchen, Den"):
            anyio.run(server.resolve_room, "Garage")


def test_rooms_are_cached_and_refreshed_on_a_miss():
    player = FakePlayer(rooms=["Kitchen"])

    async def run():
        await server.resolve_room("Kitchen")
        await server.resolve_room("kitchen")
        player._rooms.append("Garage")
        return await server.resolve_room("Garage")
    with _players(player):
        assert anyio.run(run) == (player, "Garage")
    assert player.calls.count(("rooms",)) == 2


def test_default_room_and_failing_backend():
    class Broken(FakePlayer):
        async def rooms(self):
            raise OSError("no multicast")
    with _players(Broken("Cast"), FakePlayer(rooms=["Kitchen"])), \
         patch.object(server, "DEFAULT_ROOM", "Kitchen"):
        assert anyio.run(server.resolve_room, None)[1] == "Kitchen"
    with _players(FakePlayer()), patch.object(server, "DEFAULT_ROOM", ""), \
         pytest.raises(ValueError, match="DEFAULT_ROOM is not set"):
        anyio.run(server.resolve_room, None)


def test_make_players_rejects_unknown_backends():
    assert [p.kind for p in server.make_players("sonos, DLNA")] == ["Sonos", "DLNA"]
    with pytest.raises(SystemExit, match="airplay"):
        server.make_players("sonos,airplay")


def _media(*items):
    seen = []

    async def gather(given_items, playlist_url):
        seen.append((given_items, playlist_url))
        return list(items)
    return seen, patch.object(server, "gather_media", gather)


A = MediaItem("https://8.8.8.8/a.mp3", "One", "Show")


def test_play_media_hands_the_room_what_was_gathered():
    player = FakePlayer()
    seen, gather = _media(A)
    with gather, _players(player):
        out = anyio.run(lambda: server.play_media(room="kitchen", playlist_url="https://p", volume=30))
    assert seen == [(None, "https://p")]
    assert player.calls[-1] == ("play", "Kitchen", [A], 30)
    assert out == "Playing 1 items in Kitchen:\n1. Show: One"


def test_nothing_playable_touches_no_speaker():
    player = FakePlayer()
    _, gather = _media()
    with gather, _players(player), patch.object(server, "DEFAULT_ROOM", "Kitchen"):
        assert anyio.run(lambda: server.play_media(playlist_url="https://p")) == server.NOTHING
        assert anyio.run(lambda: server.queue_media(playlist_url="https://p")) == server.NOTHING
    assert [c[0] for c in player.calls] == ["rooms"]


def test_an_unknown_room_fails_before_fetching_anything():
    seen, gather = _media(A)
    with gather, _players(FakePlayer()), pytest.raises(ValueError, match="No speaker named 'Kitchn'"):
        anyio.run(lambda: server.play_media(room="Kitchn", playlist_url="https://p"))
    assert seen == []


@pytest.mark.parametrize("position,outcome,expected", [
    ("end", ("Kitchen", 4, "waiting"), "Queued 1 items in Kitchen to play after what is queued:\n4. Show: One"),
    ("next", ("Kitchen", 2, "waiting"), "Queued 1 items in Kitchen to play next:\n2. Show: One"),
    ("end", ("Kitchen", 1, "started"), "Playing 1 items in Kitchen:\n1. Show: One"),
    ("end", ("Kitchen", 4, "elsewhere"), "play_queue_item(4) starts them"),
    ("next", ("Kitchen", 0, "busy"), "Nothing queued: Kitchen is playing from another app"),
])
def test_queue_media_outcomes(position, outcome, expected):
    player = FakePlayer(outcome=outcome)
    _, gather = _media(A)
    with gather, _players(player), patch.object(server, "DEFAULT_ROOM", "Kitchen"):
        out = anyio.run(lambda: server.queue_media(items=[MediaInput(url=A.url)], position=position))
    assert expected in out
    assert player.calls[-1] == ("append", "Kitchen", [A], position == "next")


def test_tool_schema_caps_items_and_offers_next():
    tools = {t.name: t for t in anyio.run(server.mcp.list_tools)}
    assert set(tools) == {"play_media", "queue_media", "get_queue", "remove_from_queue",
                          "play_queue_item", "stop", "skip", "list_rooms"}
    props = tools["queue_media"].inputSchema["properties"]
    items = next(s for s in props["items"]["anyOf"] if s.get("type") == "array")
    assert items["maxItems"] == 100
    assert props["position"]["enum"] == ["end", "next"]


def _post(app, headers=None):
    async def call():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.post("/mcp", headers=headers or {}, json={})
    return anyio.run(call)


async def _ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.parametrize("headers", [None, {"Authorization": "Bearer wrong"},
                                     {"Authorization": "secret"}])
def test_bearer_auth_refuses_a_missing_or_wrong_token(headers):
    response = _post(server.BearerAuth(_ok, "secret"), headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_bearer_auth_lets_the_right_token_through():
    assert _post(server.BearerAuth(_ok, "secret"), {"Authorization": "Bearer secret"}).text == "ok"


def test_http_app_is_only_wrapped_when_a_token_is_set():
    with patch.object(server, "TOKEN", ""):
        assert not isinstance(server.http_app(), server.BearerAuth)
    with patch.object(server, "TOKEN", "t"):
        assert isinstance(server.http_app(), server.BearerAuth)


def test_a_backend_that_finds_nothing_keeps_its_rooms_and_its_names():
    sonos, cast = FakePlayer("Sonos", ["Kitchen"]), FakePlayer("Cast", ["Kitchen"])
    with _players(sonos, cast):
        assert anyio.run(server.resolve_room, "kitchen")[0] is sonos
        sonos._rooms = []
        anyio.run(server.room_map, True)
        assert anyio.run(server.resolve_room, "kitchen")[0] is sonos


def test_a_public_host_without_a_secret_or_token_refuses_to_start():
    with patch.object(server, "PUBLIC_HOST", "x.ts.net"), patch.object(server, "PATH_SECRET", ""), \
            patch.object(server, "TOKEN", ""), patch("sys.argv", ["roomcast-mcp"]), \
            patch.object(server.uvicorn, "run") as run, pytest.raises(SystemExit, match="refusing"):
        server.main()
    run.assert_not_called()
