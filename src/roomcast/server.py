"""MCP server that plays audio on the speakers at home.

Runs on a machine on the same LAN as the speakers and drives them directly: Sonos with SoCo,
Google Cast with pychromecast, other DLNA renderers with async-upnp-client. It knows nothing
about where the audio comes from; the caller hands it URLs, or one playlist URL. No speaker
cloud account or developer integration is involved.
"""
import argparse
import asyncio
import hmac
import logging
import os
import time
from typing import Annotated, Any, Literal

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from roomcast.media import MAX_ITEMS, MediaInput, gather_media
from roomcast.players.base import MediaItem, Player
from roomcast.players.cast import CastPlayer
from roomcast.players.dlna import DlnaPlayer
from roomcast.players.sonos import SonosPlayer

logger = logging.getLogger("roomcast")

DEFAULT_ROOM = os.environ.get("ROOMCAST_DEFAULT_ROOM", "")
PATH_SECRET = os.environ.get("ROOMCAST_PATH_SECRET", "")
TOKEN = os.environ.get("ROOMCAST_TOKEN", "")
PUBLIC_HOST = os.environ.get("ROOMCAST_PUBLIC_HOST", "")
HOST = os.environ.get("ROOMCAST_HOST", "127.0.0.1")
PORT = int(os.environ.get("ROOMCAST_PORT", "8765"))
BACKENDS = {"sonos": SonosPlayer, "cast": CastPlayer, "dlna": DlnaPlayer}
ROOMS_TTL_SECONDS = 60


def make_players(spec: str) -> list[Player]:
    kinds = [k.strip().lower() for k in spec.split(",") if k.strip()]
    if unknown := [k for k in kinds if k not in BACKENDS]:
        raise SystemExit(f"ROOMCAST_PLAYERS has unknown backends {unknown}; known: {', '.join(BACKENDS)}")
    return [BACKENDS[k]() for k in kinds]


PLAYERS = make_players(os.environ.get("ROOMCAST_PLAYERS", "sonos,cast,dlna"))


INSTRUCTIONS = """Plays audio on the user's speakers at home (Sonos, Google Cast and DLNA;
list_rooms names them). It plays whatever URLs it is given and fetches nothing else itself.

Pass audio files or streams as items, or one M3U playlist or stream URL as playlist_url. When
another tool returns a playlist URL for what the user asked to hear (a news briefing, podcast
episodes), pass that as playlist_url rather than copying many long item URLs.

queue_media adds to what is playing instead of replacing it, at the end or, with
position="next", right after the current item. get_queue shows the queue with positions,
which remove_from_queue and play_queue_item take."""


def _transport_security() -> TransportSecuritySettings:
    if not PUBLIC_HOST:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[PUBLIC_HOST, f"{PUBLIC_HOST}:*", "localhost:*", "127.0.0.1:*"],
    )


# Chat connectors (claude.ai) cannot send a header, so a public endpoint for them lives under an
# unguessable path; every other path 404s. Leaking it lets someone play public audio on your
# speakers, nothing more. Clients that can send a header can use ROOMCAST_TOKEN as well.
mcp = FastMCP(
    "Roomcast",
    instructions=INSTRUCTIONS,
    host=HOST,
    port=PORT,
    stateless_http=True,
    streamable_http_path=f"/{PATH_SECRET}/mcp" if PATH_SECRET else "/mcp",
    transport_security=_transport_security(),
)


# lower-cased name -> (backend, its handle for the room, the name list_rooms shows)
_rooms: dict[str, tuple[Player, Any, str]] = {}
_rooms_at = float("-inf")
# Each backend's last non-empty discovery, by kind.
_known: dict[str, dict[str, Any]] = {}


async def _backend_rooms(player: Player) -> dict[str, Any]:
    try:
        return await player.rooms()
    except Exception as e:
        logger.warning("discovery_failed backend=%s error=%r", player.kind, e)
        return {}


async def room_map(refresh: bool = False) -> dict[str, tuple[Player, Any, str]]:
    """Every backend's rooms, cached: Sonos and DLNA discovery take seconds each.

    Every room answers to `kind:name`. A plain name two backends share belongs to the one
    listed first in ROOMCAST_PLAYERS, so a default room keeps working when another device
    takes its name; the other is shown as `kind:name`. A backend whose discovery fails or
    comes back empty keeps its previous rooms, or one missed SSDP answer would hand its
    names to another backend's devices."""
    global _rooms, _rooms_at
    if refresh or time.monotonic() - _rooms_at > ROOMS_TTL_SECONDS:
        found = await asyncio.gather(*(_backend_rooms(p) for p in PLAYERS))
        rooms = {}
        for player, handles in zip(PLAYERS, found, strict=True):
            if handles:
                _known[player.kind] = handles
            else:
                handles = _known.get(player.kind, {})
            for name, handle in sorted(handles.items(), key=lambda item: item[0].lower()):
                qualified = f"{player.kind.lower()}:{name}"
                if name.lower() in rooms:
                    rooms[qualified.lower()] = (player, handle, qualified)
                else:
                    rooms[name.lower()] = rooms[qualified.lower()] = (player, handle, name)
        _rooms, _rooms_at = rooms, time.monotonic()
    return _rooms


def _shown(rooms: dict[str, tuple[Player, Any, str]]) -> list[tuple[str, str]]:
    return sorted({(shown, player.kind) for player, _, shown in rooms.values()},
                  key=lambda r: r[0].lower())


async def resolve_room(room: str | None) -> tuple[Player, Any]:
    name = (room or DEFAULT_ROOM).strip()
    if not name:
        raise ValueError("No room given and ROOMCAST_DEFAULT_ROOM is not set")
    for refresh in (False, True):
        rooms = await room_map(refresh)
        if hit := rooms.get(name.lower()):
            return hit[0], hit[1]
    known = ", ".join(shown for shown, _ in _shown(rooms)) or "none"
    raise ValueError(f"No speaker named {name!r}. Known: {known}")


def _listing(items: list[MediaItem], start: int = 1) -> str:
    return "\n".join(f"{i}. {b.label}" for i, b in enumerate(items, start))


Items = Annotated[list[MediaInput] | None, Field(
    min_length=1, max_length=MAX_ITEMS, description="Audio files or streams, in play order")]
PlaylistUrl = Annotated[str | None, Field(max_length=4096, description=(
    "One URL instead of items: an M3U playlist, or a single audio file or stream"))]
Room = Annotated[str | None, Field(
    description="Room or speaker name from list_rooms; omit for the default room")]
NOTHING = "Nothing to play: none of that is a public http(s) address the speakers may fetch."


@mcp.tool()
async def play_media(room: Room = None, items: Items = None, playlist_url: PlaylistUrl = None,
                     volume: Annotated[int | None, Field(ge=0, le=100)] = None) -> str:
    """Play audio on a speaker, one item after another, replacing whatever the room was
    playing. Pass items or playlist_url. volume is 0-100; omit to leave it as it is."""
    player, device = await resolve_room(room)
    media = await gather_media(items, playlist_url)
    if not media:
        return NOTHING
    name = await player.play(device, media, volume)
    return f"Playing {len(media)} items in {name}:\n{_listing(media)}"


@mcp.tool()
async def queue_media(room: Room = None, items: Items = None, playlist_url: PlaylistUrl = None,
                      position: Annotated[Literal["end", "next"], Field(description=(
                          '"next" plays them right after the current item'))] = "end") -> str:
    """Add audio to the room's queue without interrupting what is playing.

    Use for "add X", "then play Y", "play Z next". The items start right away only if the room
    is idle; anything playing, including TV, radio or another app, is left alone."""
    player, device = await resolve_room(room)
    media = await gather_media(items, playlist_url)
    if not media:
        return NOTHING
    name, first, outcome = await player.append(device, media, position == "next")
    listing = _listing(media, first)
    if outcome == "started":
        return f"Playing {len(media)} items in {name}:\n{listing}"
    if outcome == "waiting":
        where = "next" if position == "next" else "after what is queued"
        return f"Queued {len(media)} items in {name} to play {where}:\n{listing}"
    if outcome == "busy":
        return (f"Nothing queued: {name} is playing from another app, whose queue roomcast "
                "cannot add to. play_media would replace what is playing.")
    return (f"Queued {len(media)} items in {name}, which is playing something else, so they "
            f"have not started; play_queue_item({first}) starts them:\n{listing}")


@mcp.tool()
async def get_queue(room: Room = None) -> str:
    """Show the room's queue with numbered positions and what is playing now."""
    player, device = await resolve_room(room)
    return await player.queue(device)


@mcp.tool()
async def remove_from_queue(positions: Annotated[list[int], Field(min_length=1)],
                            room: Room = None) -> str:
    """Remove items from the room's queue by their positions in get_queue (1-based). Call
    get_queue first: positions shift after every removal and every queue_media. Returns the
    queue as it is afterwards."""
    player, device = await resolve_room(room)
    return await player.remove(device, positions)


@mcp.tool()
async def play_queue_item(position: Annotated[int, Field(ge=1)], room: Room = None) -> str:
    """Jump to a position in the room's queue (1-based, from get_queue) and play from there."""
    player, device = await resolve_room(room)
    return f"Playing item {position} in {await player.jump(device, position)}."


@mcp.tool()
async def stop(room: Room = None) -> str:
    """Stop playback in a room (default room if omitted)."""
    player, device = await resolve_room(room)
    return f"Stopped {await player.stop(device)}."


@mcp.tool()
async def skip(room: Room = None) -> str:
    """Skip to the next item in a room's queue (default room if omitted)."""
    player, device = await resolve_room(room)
    return f"Now playing: {await player.next(device)}"


@mcp.tool()
async def list_rooms() -> str:
    """List the rooms and speakers audio can be played in, with their kind."""
    rooms = _shown(await room_map(refresh=True))
    default = f" Default: {DEFAULT_ROOM}." if DEFAULT_ROOM else ""
    listed = ", ".join(f"{shown} ({kind})" for shown, kind in rooms)
    return f"{listed or 'No speakers found'}.{default}"


class BearerAuth:
    """Requires `Authorization: Bearer <token>` on every HTTP request."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            given = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(given, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"www-authenticate", b"Bearer"),
                                        (b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


def http_app():
    app = mcp.streamable_http_app()
    return BearerAuth(app, TOKEN) if TOKEN else app


def main():
    parser = argparse.ArgumentParser(prog="roomcast-mcp", description=__doc__.splitlines()[0])
    parser.add_argument("--stdio", action="store_true",
                        help="serve one local client over stdin/stdout instead of HTTP")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.stdio:
        mcp.run(transport="stdio")
        return
    if PUBLIC_HOST and not PATH_SECRET and not TOKEN:
        raise SystemExit("ROOMCAST_PUBLIC_HOST is set but neither ROOMCAST_PATH_SECRET nor "
                         "ROOMCAST_TOKEN is: refusing to serve a public endpoint anyone can use")
    if not PATH_SECRET and not TOKEN:
        logger.warning("Neither ROOMCAST_PATH_SECRET nor ROOMCAST_TOKEN is set: anyone who can "
                       "reach %s:%s can play audio on your speakers", HOST, PORT)
    uvicorn.run(http_app(), host=HOST, port=PORT)


if __name__ == "__main__":
    main()
