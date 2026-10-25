"""Google Cast (Chromecast, Nest speakers and displays, Cast groups) with pychromecast.

The queue lives on the device, in the Default Media Receiver's media session. Another app
(Spotify, YouTube Music) runs a session of its own, which roomcast cannot add to."""
import os
import threading
import time
from uuid import UUID

import anyio
import pychromecast
import zeroconf
from pychromecast.config import APP_MEDIA_RECEIVER
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.models import CastInfo
from pychromecast.response_handler import WaitResponse

from roomcast.players.base import MediaItem, Outcome, check_positions, parse_list

# Device IPs to poll as well as mDNS, for networks where multicast does not get through.
CAST_HOSTS = parse_list(os.environ.get("ROOMCAST_CAST_HOSTS", ""))
DISCOVERY_SECONDS = 3
TIMEOUT = 10
MAX_LISTED = 100
# A Cast channel message is capped at 64KB, and resolved CDN URLs can be long.
CHUNK = 20
ACTIVE = ("PLAYING", "BUFFERING", "PAUSED")
PLAYING = ("PLAYING", "BUFFERING")
ERRORS = ("INVALID_REQUEST", "LOAD_FAILED", "LOAD_CANCELLED", "ERROR")
CONTENT_TYPES = {"audio/mpeg", "audio/mp4", "audio/aac", "audio/ogg", "audio/webm", "audio/flac",
                 "audio/wav"}
HLS_TYPES = {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl",
             "audio/x-mpegurl"}
HLS = "application/x-mpegURL"


def content_type(declared: str | None) -> str:
    """Feeds declare types like audio/x-mpeg, and the receiver picks its player by contentType,
    so anything it may not know goes out as audio/mpeg."""
    value = (declared or "").lower()
    if value in HLS_TYPES:
        return HLS
    if value in ("audio/x-m4a", "audio/m4a"):
        return "audio/mp4"
    return value if value in CONTENT_TYPES else "audio/mpeg"


def queue_item(b: MediaItem) -> dict:
    kind = content_type(b.media_type)
    return {
        "media": {
            "contentId": b.url,
            "contentType": kind,
            "streamType": "LIVE" if kind == HLS else "BUFFERED",
            "metadata": {"metadataType": 0, "title": b.title,
                         **({"subtitle": b.subtitle} if b.subtitle else {})},
        },
        "autoplay": True,
        "startTime": 0,
        "preloadTime": 5,
    }


def item_label(item: dict) -> str:
    media = item.get("media") or {}
    meta = media.get("metadata") or {}
    title = meta.get("title") or media.get("contentId", "?")
    return f"{meta['subtitle']}: {title}" if meta.get("subtitle") else title


def name(cast: pychromecast.Chromecast) -> str:
    return cast.name or cast.uri


def request(cast: pychromecast.Chromecast, message: dict) -> dict:
    handler = WaitResponse(TIMEOUT, message["type"])
    cast.media_controller.send_message(message, inc_session_id=True,
                                       callback_function=handler.callback)
    handler.wait_response()
    response = handler.response or {}
    if response.get("type") in ERRORS:
        raise RuntimeError(f"{name(cast)} refused {message['type']}: "
                           f"{response.get('reason') or response['type']}")
    return response


def session(cast: pychromecast.Chromecast) -> dict | None:
    """The status of our media session, or None when the Default Media Receiver plays nothing."""
    if cast.app_id != APP_MEDIA_RECEIVER:
        return None
    status = (request(cast, {"type": "GET_STATUS"}).get("status") or [None])[0]
    if not status or status.get("playerState") not in ACTIVE:
        return None
    return status


def other_app_playing(cast: pychromecast.Chromecast) -> bool:
    """Asks the app itself: the cached media status is still UNKNOWN right after connecting.
    An app without the media namespace (screen mirroring, a game) cannot be asked, so it
    counts as busy rather than being replaced."""
    if cast.is_idle or cast.app_id == APP_MEDIA_RECEIVER:
        return False
    if not cast.media_controller.is_active:
        return True
    status = (request(cast, {"type": "GET_STATUS"}).get("status") or [{}])[0]
    return status.get("playerState") in PLAYING


def item_ids(cast: pychromecast.Chromecast, status: dict) -> list[int]:
    response = request(cast, {"type": "QUEUE_GET_ITEM_IDS",
                              "mediaSessionId": status["mediaSessionId"]})
    return response.get("itemIds") or []


def _chunks(items: list[MediaItem]) -> list[list[dict]]:
    queued = [queue_item(b) for b in items]
    return [queued[i:i + CHUNK] for i in range(0, len(queued), CHUNK)]


def _insert(cast: pychromecast.Chromecast, session_id: int, chunks: list[list[dict]],
            before: int | None = None) -> None:
    for chunk in chunks:
        message = {"type": "QUEUE_INSERT", "mediaSessionId": session_id, "items": chunk}
        if before is not None:
            message["insertBefore"] = before
        request(cast, message)


def load(cast: pychromecast.Chromecast, items: list[MediaItem], volume: int | None = None) -> str:
    if cast.app_id != APP_MEDIA_RECEIVER:
        cast.start_app(APP_MEDIA_RECEIVER)
    if volume is not None:
        cast.set_volume(volume / 100)
    first, *rest = _chunks(items)
    response = request(cast, {"type": "QUEUE_LOAD", "items": first, "startIndex": 0,
                              "repeatMode": "REPEAT_OFF"})
    if rest:
        status = (response.get("status") or [{}])[0]
        if "mediaSessionId" not in status:
            status = session(cast) or {}
        if "mediaSessionId" not in status:
            raise RuntimeError(f"{name(cast)} did not start a media session")
        _insert(cast, status["mediaSessionId"], rest)
    return name(cast)


def append(cast: pychromecast.Chromecast, items: list[MediaItem],
           after_current: bool = False) -> tuple[str, int, Outcome]:
    if status := session(cast):
        ids = item_ids(cast, status)
        first, before = len(ids) + 1, None
        current = status.get("currentItemId")
        if after_current and current in ids and ids.index(current) + 1 < len(ids):
            before = ids[ids.index(current) + 1]
            first = ids.index(current) + 2
        _insert(cast, status["mediaSessionId"], _chunks(items), before)
        return name(cast), first, "waiting"
    if other_app_playing(cast):
        return name(cast), 0, "busy"
    return load(cast, items), 1, "started"


def describe(cast: pychromecast.Chromecast) -> str:
    if other_app_playing(cast):
        return (f"{name(cast)} is playing from {cast.app_display_name or 'another app'}, "
                "which has its own queue roomcast cannot see.")
    status = session(cast)
    if status is None or not (ids := item_ids(cast, status)):
        return f"The queue in {name(cast)} is empty."
    items = request(cast, {"type": "QUEUE_GET_ITEMS", "mediaSessionId": status["mediaSessionId"],
                           "itemIds": ids[:MAX_LISTED]}).get("items") or []
    by_id = {item.get("itemId"): item for item in items}
    playing = status["playerState"] in PLAYING
    lines = []
    for position, item_id in enumerate(ids[:MAX_LISTED], 1):
        line = f"{position}. {item_label(by_id.get(item_id) or {})}"
        if item_id == status.get("currentItemId"):
            line += " <- now playing" if playing else " <- current (paused or stopped)"
        lines.append(line)
    if len(ids) > MAX_LISTED:
        lines.append(f"... and {len(ids) - MAX_LISTED} more")
    return f"Queue in {name(cast)}:\n" + "\n".join(lines)


def _queue(cast: pychromecast.Chromecast) -> tuple[dict, list[int]]:
    status = session(cast)
    if status is None:
        raise ValueError(f"No queue roomcast started is active in {name(cast)}")
    return status, item_ids(cast, status)


def remove(cast: pychromecast.Chromecast, positions: list[int]) -> str:
    status, ids = _queue(cast)
    check_positions(positions, len(ids))
    request(cast, {"type": "QUEUE_REMOVE", "mediaSessionId": status["mediaSessionId"],
                   "itemIds": [ids[p - 1] for p in sorted(set(positions))]})
    return describe(cast)


def jump(cast: pychromecast.Chromecast, position: int) -> str:
    status, ids = _queue(cast)
    check_positions([position], len(ids))
    request(cast, {"type": "QUEUE_UPDATE", "mediaSessionId": status["mediaSessionId"],
                   "currentItemId": ids[position - 1]})
    return name(cast)


def skip(cast: pychromecast.Chromecast) -> str:
    status, ids = _queue(cast)
    current = status.get("currentItemId")
    if not ids or current == ids[-1]:
        stop(cast)
        return "nothing, that was the last one; stopped"
    following = ids[ids.index(current) + 1] if current in ids else ids[0]
    request(cast, {"type": "QUEUE_UPDATE", "mediaSessionId": status["mediaSessionId"],
                   "currentItemId": following})
    items = request(cast, {"type": "QUEUE_GET_ITEMS", "mediaSessionId": status["mediaSessionId"],
                           "itemIds": [following]}).get("items") or [{}]
    return item_label(items[0])


def stop(cast: pychromecast.Chromecast) -> str:
    """Asks for the session first: right after connecting pychromecast has no media status
    cached, and its own stop() then sends nothing."""
    if cast.is_idle or not cast.media_controller.is_active:
        return name(cast)
    status = (request(cast, {"type": "GET_STATUS"}).get("status") or [{}])[0]
    if "mediaSessionId" in status and status.get("playerState") in ACTIVE:
        request(cast, {"type": "STOP", "mediaSessionId": status["mediaSessionId"]})
    return name(cast)


class CastPlayer:
    kind = "Cast"

    def __init__(self, hosts: list[str] | None = None):
        self._hosts = CAST_HOSTS if hosts is None else hosts
        self._lock = threading.Lock()
        self._browser: CastBrowser | None = None
        self._zconf: zeroconf.Zeroconf | None = None
        self._casts: dict[UUID, pychromecast.Chromecast] = {}

    def _devices(self) -> list[CastInfo]:
        with self._lock:
            if self._browser is None:
                self._zconf = zeroconf.Zeroconf()
                self._browser = CastBrowser(SimpleCastListener(), self._zconf, self._hosts)
                self._browser.start_discovery()
                time.sleep(DISCOVERY_SECONDS)
            browser = self._browser
        return list(browser.devices.values())

    def by_name(self) -> dict[str, CastInfo]:
        return {d.friendly_name: d for d in self._devices() if d.friendly_name}

    def device(self, info: CastInfo) -> pychromecast.Chromecast:
        with self._lock:
            cast = self._casts.get(info.uuid)
            if cast is None:
                cast = pychromecast.get_chromecast_from_cast_info(info, self._zconf)
                self._casts[info.uuid] = cast
        cast.wait(timeout=TIMEOUT)
        return cast

    async def _run(self, func, room: CastInfo, *args):
        return await anyio.to_thread.run_sync(lambda: func(self.device(room), *args))

    async def rooms(self):
        return await anyio.to_thread.run_sync(self.by_name)

    async def play(self, room, items, volume):
        return await self._run(load, room, items, volume)

    async def append(self, room, items, after_current=False):
        return await self._run(append, room, items, after_current)

    async def queue(self, room):
        return await self._run(describe, room)

    async def remove(self, room, positions):
        return await self._run(remove, room, positions)

    async def jump(self, room, position):
        return await self._run(jump, room, position)

    async def stop(self, room):
        return await self._run(stop, room)

    async def next(self, room):
        return await self._run(skip, room)
