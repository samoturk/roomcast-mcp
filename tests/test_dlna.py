import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from async_upnp_client.profiles.dlna import TransportState

from roomcast.players import dlna
from roomcast.players.base import MediaItem

ROOM = "Living room TV"


def _bulletins(n, prefix="u"):
    return [MediaItem(f"https://{prefix}/{i}.mp3", f"t{i}", f"F{i}") for i in range(1, n + 1)]


class FakeRenderer:
    name = ROOM
    has_next_transport_uri = True
    has_volume_level = True

    def __init__(self):
        self.calls = []
        self.av_transport_uri = None
        self.current_track_uri = None
        self.transport_state = TransportState.STOPPED
        self.media_position = None
        self.media_duration = None

    async def async_set_transport_uri(self, url, title, meta):
        self.calls.append(("uri", url))
        assert f"<dc:title>{title.split(': ', 1)[1]}</dc:title>" in meta
        self.av_transport_uri = url

    async def async_wait_for_can_play(self):
        pass

    async def async_play(self):
        self.calls.append(("play",))
        self.transport_state = TransportState.PLAYING

    async def async_set_next_transport_uri(self, url, title, meta):
        self.calls.append(("next", url))

    async def async_stop(self):
        self.calls.append(("stop",))
        self.transport_state = TransportState.STOPPED

    async def async_update(self, do_ping=True):
        pass

    async def async_set_volume_level(self, volume):
        self.calls.append(("volume", volume))

    def since(self, mark):
        return self.calls[mark:]


def run(scenario):
    """Runs scenario(player, renderer, queue) on one event loop, so the watcher task lives."""
    renderer = FakeRenderer()
    player = dlna.DlnaPlayer(hosts=[], factory=MagicMock())

    async def main():
        try:
            return await scenario(player, renderer)
        finally:
            for q in player._queues.values():
                await player._halt(q)
    return anyio.run(main), renderer, player


def test_play_sets_the_volume_then_the_first_and_arms_the_next():
    async def scenario(player, renderer):
        assert await player.play(renderer, _bulletins(3), volume=25) == ROOM
        return player._queues[ROOM.lower()].active
    active, renderer, _ = run(scenario)
    assert active
    assert renderer.calls == [("volume", 0.25), ("uri", "https://u/1.mp3"), ("play",),
                              ("next", "https://u/2.mp3")]


def test_step_follows_the_renderer_onto_the_armed_next():
    async def scenario(player, renderer):
        await player.play(renderer, _bulletins(3), None)
        q = player._queues[ROOM.lower()]
        mark = len(renderer.calls)
        renderer.av_transport_uri = "https://u/2.mp3"
        assert await player._step(renderer, q) is True
        return q.index, renderer.since(mark)
    (index, calls), _, _ = run(scenario)
    assert index == 1
    assert calls == [("next", "https://u/3.mp3")]


@pytest.mark.parametrize("position,duration,advances", [
    (292, 300, True),
    (None, None, True),
    (30, 300, False),
])
def test_step_advances_only_when_the_track_ran_out(position, duration, advances):
    async def scenario(player, renderer):
        await player.play(renderer, _bulletins(2), None)
        q = player._queues[ROOM.lower()]
        renderer.media_position, renderer.media_duration = position, duration
        await player._step(renderer, q)
        renderer.transport_state = TransportState.STOPPED
        mark = len(renderer.calls)
        keep_watching = await player._step(renderer, q)
        return keep_watching, q.index, renderer.since(mark)
    (keep_watching, index, calls), _, _ = run(scenario)
    assert keep_watching is advances
    if advances:
        assert index == 1 and calls[:2] == [("uri", "https://u/2.mp3"), ("play",)]
    else:
        assert index == 0 and calls == []


def test_step_lets_go_when_another_source_takes_over_or_the_queue_ends():
    async def scenario(player, renderer):
        await player.play(renderer, _bulletins(1), None)
        q = player._queues[ROOM.lower()]
        await player._step(renderer, q)
        renderer.transport_state = TransportState.STOPPED
        ended = await player._step(renderer, q)
        await player.play(renderer, _bulletins(2), None)
        q = player._queues[ROOM.lower()]
        renderer.av_transport_uri = "http://tv/hdmi1"
        switched = await player._step(renderer, q)
        return ended, switched
    (ended, switched), _, _ = run(scenario)
    assert ended is False and switched is False


def test_watcher_moves_the_queue_along_by_itself():
    async def scenario(player, renderer):
        await player.play(renderer, _bulletins(2), None)
        await asyncio.sleep(0.05)
        renderer.transport_state = TransportState.STOPPED
        for _ in range(100):
            await asyncio.sleep(0.01)
            if renderer.av_transport_uri == "https://u/2.mp3":
                break
        return player._queues[ROOM.lower()].index
    with patch.object(dlna, "POLL_SECONDS", 0.01):
        index, renderer, _ = run(scenario)
    assert index == 1
    assert ("uri", "https://u/2.mp3") in renderer.calls


def test_append_outcomes():
    async def scenario(player, renderer):
        await player.play(renderer, _bulletins(1), None)
        mark = len(renderer.calls)
        waiting = await player.append(renderer, _bulletins(1, "w"))
        armed = renderer.since(mark)
        await player.stop(renderer)
        renderer.transport_state = TransportState.PLAYING
        renderer.av_transport_uri = "http://radio/stream"
        elsewhere = await player.append(renderer, _bulletins(1, "e"))
        renderer.transport_state = TransportState.STOPPED
        started = await player.append(renderer, _bulletins(1, "s"))
        return waiting, armed, elsewhere, started, player._queues[ROOM.lower()].index
    (waiting, armed, elsewhere, started, index), renderer, _ = run(scenario)
    assert waiting == (ROOM, 2, "waiting") and armed == [("next", "https://w/1.mp3")]
    assert elsewhere == (ROOM, 3, "elsewhere")
    assert started == (ROOM, 4, "started") and index == 3
    assert renderer.av_transport_uri == "https://s/1.mp3"


def test_remove_rearms_or_moves_on():
    async def scenario(player, renderer):
        await player.play(renderer, _bulletins(4), None)
        mark = len(renderer.calls)
        after_next = await player.remove(renderer, [2])
        rearmed = renderer.since(mark)
        mark = len(renderer.calls)
        await player.remove(renderer, [1])
        moved_on = renderer.since(mark)
        mark = len(renderer.calls)
        await player.remove(renderer, [1, 2])
        return after_next, rearmed, moved_on, renderer.since(mark), player._queues[ROOM.lower()]
    (after_next, rearmed, moved_on, emptied, q), _, _ = run(scenario)
    assert after_next == f"Queue in {ROOM}:\n1. F1: t1 <- now playing\n2. F3: t3\n3. F4: t4"
    assert rearmed == [("next", "https://u/3.mp3")]
    assert moved_on[:2] == [("uri", "https://u/3.mp3"), ("play",)]
    assert emptied == [("stop",)]
    assert q.items == [] and not q.active


def test_remove_validates_positions():
    async def scenario(player, renderer):
        await player.play(renderer, _bulletins(2), None)
        with pytest.raises(ValueError, match="No queue position 3"):
            await player.remove(renderer, [3])
    run(scenario)


def test_jump_next_and_stop():
    async def scenario(player, renderer):
        await player.play(renderer, _bulletins(3), None)
        await player.jump(renderer, 3)
        at_last = await player.next(renderer)
        q = player._queues[ROOM.lower()]
        with pytest.raises(ValueError, match="Nothing roomcast started is playing"):
            await player.next(renderer)
        await player.jump(renderer, 1)
        following = await player.next(renderer)
        await player.stop(renderer)
        return at_last, following, q.active
    (at_last, following, active), renderer, _ = run(scenario)
    assert at_last == "nothing, that was the last one; stopped"
    assert following == "F2: t2"
    assert not active and renderer.calls[-1] == ("stop",)


def _discovery(search, devices):
    """A DlnaPlayer whose SSDP search and description fetches are canned."""
    async def create(location):
        if location not in devices:
            raise OSError("refused")
        return devices[location]

    factory = MagicMock()
    factory.async_create_device = AsyncMock(side_effect=create)

    def renderer(device, handler):
        dmr = MagicMock()
        dmr.name = device.manufacturer
        return dmr
    fake_dmr = MagicMock(side_effect=renderer)
    fake_dmr.async_search = AsyncMock(side_effect=search)
    fake_dmr.is_profile_device.return_value = True
    return factory, fake_dmr


def test_discovery_skips_sonos_and_unreadable_devices_and_loads_each_once():
    found = [{"location": "http://tv/desc.xml"}, {"location": "http://sonos/desc.xml"}]
    factory, fake_dmr = _discovery(lambda timeout: found, {
        "http://tv/desc.xml": MagicMock(manufacturer="Samsung"),
        "http://sonos/desc.xml": MagicMock(manufacturer="Sonos, Inc."),
    })
    player = dlna.DlnaPlayer(hosts=["http://broken/desc.xml"], factory=factory)

    async def twice():
        first = await player.rooms()
        return first, await player.rooms()
    with patch.object(dlna, "DmrDevice", fake_dmr):
        first, second = anyio.run(twice)
    assert list(first) == ["Samsung"] and second["Samsung"] is first["Samsung"]
    loaded = [c.args[0] for c in factory.async_create_device.call_args_list]
    assert sorted(loaded) == ["http://broken/desc.xml", "http://broken/desc.xml",
                              "http://sonos/desc.xml", "http://tv/desc.xml"]


def test_static_hosts_survive_a_failing_search():
    def search(timeout):
        raise OSError("multicast unavailable")
    factory, fake_dmr = _discovery(search, {"http://tv/desc.xml": MagicMock(manufacturer="LG")})
    player = dlna.DlnaPlayer(hosts=["http://tv/desc.xml"], factory=factory)
    with patch.object(dlna, "DmrDevice", fake_dmr):
        assert list(anyio.run(player.rooms)) == ["LG"]


def test_a_repeated_episode_is_played_twice_not_skipped():
    a, b = MediaItem("https://u/a.mp3", "a", "F"), MediaItem("https://u/b.mp3", "b", "F")

    async def scenario(player, renderer):
        await player.play(renderer, [a, a, b], None)
        q = player._queues[ROOM.lower()]
        armed = renderer.calls[2]
        await player._step(renderer, q)
        index_while_first_plays = q.index
        renderer.transport_state = TransportState.STOPPED
        mark = len(renderer.calls)
        await player._step(renderer, q)
        return armed, index_while_first_plays, q.index, renderer.since(mark)
    (armed, during, after, calls), _, _ = run(scenario)
    assert armed == ("next", "")
    assert during == 0 and after == 1
    assert calls == [("uri", "https://u/a.mp3"), ("play",), ("next", "https://u/b.mp3")]


def test_watcher_survives_a_poll_that_times_out():
    async def scenario(player, renderer):
        failures = iter([TimeoutError(), TimeoutError()])

        async def flaky(do_ping=True):
            if exc := next(failures, None):
                raise exc
        renderer.async_update = flaky
        await player.play(renderer, _bulletins(1), None)
        await asyncio.sleep(0.1)
        return player._queues[ROOM.lower()].active
    with patch.object(dlna, "POLL_SECONDS", 0.01):
        active, _, _ = run(scenario)
    assert active


def test_didl_escapes_and_carries_the_type():
    xml = dlna.didl(MediaItem("https://x/a.mp3?a=1&b=2", "t", "A & B", "audio/mp4"))
    assert "https://x/a.mp3?a=1&amp;b=2" in xml
    assert 'protocolInfo="http-get:*:audio/mp4:*"' in xml
    assert "<dc:creator>A &amp; B</dc:creator>" in xml


def test_next_inserts_after_the_current_item_and_rearms():
    async def scenario(player, renderer):
        await player.play(renderer, _bulletins(3), None)
        mark = len(renderer.calls)
        result = await player.append(renderer, _bulletins(1, "n"), after_current=True)
        q = player._queues[ROOM.lower()]
        return result, [b.url for b in q.items], renderer.since(mark)
    (result, urls, calls), _, _ = run(scenario)
    assert result == (ROOM, 2, "waiting")
    assert urls == ["https://u/1.mp3", "https://n/1.mp3", "https://u/2.mp3", "https://u/3.mp3"]
    assert calls == [("next", "https://n/1.mp3")]


def test_next_first_catches_up_with_a_renderer_that_moved_on():
    async def scenario(player, renderer):
        await player.play(renderer, _bulletins(3), None)
        renderer.av_transport_uri = "https://u/2.mp3"
        result = await player.append(renderer, _bulletins(1, "n"), after_current=True)
        q = player._queues[ROOM.lower()]
        return result, q.index, [b.url for b in q.items], q.active
    (result, index, urls, active), _, _ = run(scenario)
    assert result == (ROOM, 3, "waiting") and index == 1 and active
    assert urls == ["https://u/1.mp3", "https://u/2.mp3", "https://n/1.mp3", "https://u/3.mp3"]


def test_next_on_a_stopped_queue_behaves_like_end():
    async def scenario(player, renderer):
        await player.play(renderer, _bulletins(2), None)
        await player.stop(renderer)
        return await player.append(renderer, _bulletins(1, "n"), after_current=True), renderer
    (result, renderer), _, _ = run(scenario)
    assert result == (ROOM, 3, "started")
    assert renderer.av_transport_uri == "https://n/1.mp3"
