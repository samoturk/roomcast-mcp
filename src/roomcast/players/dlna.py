"""Generic DLNA/UPnP media renderers with async-upnp-client.

A renderer has no queue, only a current URI and, on some, a next one. Roomcast holds the
queue and moves it along by polling, so the renderer's own app never sees it, and it is lost
when roomcast restarts. Polling rather than UPnP eventing, so roomcast needs no inbound
callback port on the LAN."""
import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from urllib.parse import unquote

from async_upnp_client.aiohttp import AiohttpRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.exceptions import UpnpError
from async_upnp_client.profiles.dlna import DmrDevice, TransportState
from didl_lite import didl_lite

from roomcast.players.base import MediaItem, Outcome, check_positions, parse_list

logger = logging.getLogger("roomcast.dlna")

# Device description URLs (http://ip:port/description.xml), used as well as the SSDP search.
DLNA_HOSTS = parse_list(os.environ.get("ROOMCAST_DLNA_HOSTS", ""))
SEARCH_SECONDS = 3
POLL_SECONDS = 2.0
# A renderer that stops this close to the end of a track has finished it, not been stopped.
END_SLACK_SECONDS = 10
MAX_POLL_FAILURES = 15
MAX_LISTED = 100
BUSY = (TransportState.PLAYING, TransportState.TRANSITIONING)
IDLE = (TransportState.STOPPED, TransportState.NO_MEDIA_PRESENT)


def didl(b: MediaItem) -> str:
    """Built here because DmrDevice would otherwise fetch the audio URL to sniff its type."""
    resource = didl_lite.Resource(uri=b.url,
                                  protocol_info=f"http-get:*:{b.media_type or 'audio/mpeg'}:*")
    item = didl_lite.MusicTrack(id="0", parent_id="-1", title=b.title, restricted="false",
                                creator=b.subtitle, resources=[resource])
    return didl_lite.to_xml_string(item).decode()


def same_uri(a: str | None, b: str | None) -> bool:
    return bool(a and b) and (a == b or unquote(a) == unquote(b))


def finished(position: int | None, duration: int | None) -> bool:
    return not duration or position is None or position >= duration - END_SLACK_SECONDS


@dataclass
class RoomQueue:
    items: list[MediaItem] = field(default_factory=list)
    index: int = 0
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    seen_playing: bool = False
    position: int | None = None
    duration: int | None = None

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def following(self) -> MediaItem | None:
        return self.items[self.index + 1] if self.index + 1 < len(self.items) else None


class DlnaPlayer:
    kind = "DLNA"

    def __init__(self, hosts: list[str] | None = None, factory: UpnpFactory | None = None):
        self._hosts = DLNA_HOSTS if hosts is None else hosts
        self._factory = factory or UpnpFactory(AiohttpRequester(), non_strict=True)
        # description URL -> device, or None for one that is not ours to play (Sonos, not a
        # renderer). Unreadable ones are left out so the next round tries them again.
        self._by_location: dict[str, DmrDevice | None] = {}
        self._queues: dict[str, RoomQueue] = {}

    async def _search(self) -> list[str]:
        try:
            found = await DmrDevice.async_search(timeout=SEARCH_SECONDS)
        except Exception as e:
            logger.warning("dlna_search_failed error=%r", e)
            return []
        return [info["location"] for info in found if info.get("location")]

    async def _load(self, location: str) -> None:
        try:
            device = await self._factory.async_create_device(location)
        except Exception as e:
            logger.warning("dlna_device_unreadable location=%s error=%r", location, e)
            return
        # Sonos speakers announce themselves as MediaRenderers too; the Sonos backend has them.
        if "sonos" in (device.manufacturer or "").lower() or not DmrDevice.is_profile_device(device):
            self._by_location[location] = None
        else:
            self._by_location[location] = DmrDevice(device, None)

    async def rooms(self) -> dict[str, DmrDevice]:
        locations = set(self._hosts) | set(await self._search())
        await asyncio.gather(*(self._load(loc) for loc in locations if loc not in self._by_location))
        devices = (self._by_location.get(loc) for loc in locations)
        return {d.name: d for d in devices if d}

    def _queue(self, dev: DmrDevice) -> RoomQueue:
        return self._queues.setdefault(dev.name.lower(), RoomQueue())

    async def _arm(self, dev: DmrDevice, q: RoomQueue) -> None:
        """Hands the renderer the next file, except a repeat of the current one: the watcher
        could not tell it had moved on, so that one is started when the first play stops."""
        if not dev.has_next_transport_uri:
            return
        b = q.following()
        if b and same_uri(b.url, q.items[q.index].url):
            b = None
        try:
            if b:
                await dev.async_set_next_transport_uri(b.url, b.label, didl(b))
            else:
                await dev.async_set_next_transport_uri("", "", "")
        except UpnpError as e:
            logger.info("dlna_set_next_failed device=%s error=%r", dev.name, e)

    async def _start(self, dev: DmrDevice, q: RoomQueue, index: int) -> None:
        q.index, q.seen_playing, q.position, q.duration = index, False, None, None
        b = q.items[index]
        await dev.async_set_transport_uri(b.url, b.label, didl(b))
        await dev.async_wait_for_can_play()
        await dev.async_play()
        await self._arm(dev, q)

    def _spawn(self, dev: DmrDevice, q: RoomQueue) -> None:
        q.task = asyncio.create_task(self._watch(dev, q))

    @staticmethod
    async def _halt(q: RoomQueue) -> None:
        if q.task is not None and not q.task.done():
            q.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await q.task
        q.task = None

    async def _watch(self, dev: DmrDevice, q: RoomQueue) -> None:
        failures = 0
        while True:
            await asyncio.sleep(POLL_SECONDS)
            async with q.lock:
                try:
                    await dev.async_update(do_ping=False)
                except Exception as e:
                    failures += 1
                    if failures >= MAX_POLL_FAILURES:
                        logger.warning("dlna_watch_gave_up device=%s error=%r", dev.name, e)
                        return
                    continue
                failures = 0
                try:
                    if not await self._step(dev, q):
                        return
                except Exception as e:
                    logger.warning("dlna_advance_failed device=%s error=%r", dev.name, e)
                    return

    async def _step(self, dev: DmrDevice, q: RoomQueue) -> bool:
        """One poll's worth of moving the queue along; False once there is nothing to watch."""
        uri = dev.av_transport_uri or dev.current_track_uri
        state = dev.transport_state
        current, following = q.items[q.index], q.following()
        if (following and same_uri(uri, following.url)
                and not same_uri(following.url, current.url)):
            q.index += 1
            q.seen_playing, q.position, q.duration = state == TransportState.PLAYING, None, None
            await self._arm(dev, q)
        elif uri and not same_uri(uri, current.url):
            return False
        elif state == TransportState.PLAYING:
            q.seen_playing, q.position, q.duration = True, dev.media_position, dev.media_duration
        elif state in IDLE and q.seen_playing:
            if not finished(q.position, q.duration) or following is None:
                return False
            await self._start(dev, q, q.index + 1)
        return True

    async def play(self, dev, items, volume):
        q = self._queue(dev)
        async with q.lock:
            await self._halt(q)
            q.items = list(items)
            if volume is not None and dev.has_volume_level:
                await dev.async_set_volume_level(volume / 100)
            await self._start(dev, q, 0)
            self._spawn(dev, q)
        return dev.name

    async def append(self, dev, items, after_current=False) -> tuple[str, int, Outcome]:
        q = self._queue(dev)
        async with q.lock:
            if q.active and after_current:
                # The renderer may have moved on to the armed next file since the last poll;
                # inserting before noticing would leave the watcher looking at a stranger.
                await dev.async_update(do_ping=False)
                if not await self._step(dev, q):
                    await self._halt(q)
            if q.active and after_current:
                q.items[q.index + 1:q.index + 1] = items
                await self._arm(dev, q)
                return dev.name, q.index + 2, "waiting"
            first = len(q.items) + 1
            had_following = q.following() is not None
            q.items.extend(items)
            if q.active:
                if not had_following:
                    await self._arm(dev, q)
                return dev.name, first, "waiting"
            await dev.async_update(do_ping=False)
            if dev.transport_state in BUSY:
                return dev.name, first, "elsewhere"
            await self._start(dev, q, first - 1)
            self._spawn(dev, q)
        return dev.name, first, "started"

    async def queue(self, dev):
        q = self._queues.get(dev.name.lower())
        if not q or not q.items:
            return f"The queue in {dev.name} is empty."
        playing = False
        if q.active:
            await dev.async_update(do_ping=False)
            playing = dev.transport_state == TransportState.PLAYING
        lines = []
        for position, b in enumerate(q.items[:MAX_LISTED], 1):
            line = f"{position}. {b.label}"
            if q.active and position == q.index + 1:
                line += " <- now playing" if playing else " <- current (paused or stopped)"
            lines.append(line)
        if len(q.items) > MAX_LISTED:
            lines.append(f"... and {len(q.items) - MAX_LISTED} more")
        return f"Queue in {dev.name}:\n" + "\n".join(lines)

    async def remove(self, dev, positions):
        q = self._queue(dev)
        async with q.lock:
            check_positions(positions, len(q.items))
            drop = {p - 1 for p in positions}
            following = q.following()
            current_dropped = q.active and q.index in drop
            q.index -= sum(1 for i in drop if i < q.index)
            q.items = [b for i, b in enumerate(q.items) if i not in drop]
            if current_dropped and q.index < len(q.items):
                await self._start(dev, q, q.index)
            elif current_dropped:
                await self._halt(q)
                await dev.async_stop()
            elif q.active and q.following() is not following:
                await self._arm(dev, q)
            q.index = min(q.index, max(len(q.items) - 1, 0))
        return await self.queue(dev)

    async def jump(self, dev, position):
        q = self._queue(dev)
        async with q.lock:
            check_positions([position], len(q.items))
            await self._start(dev, q, position - 1)
            if not q.active:
                self._spawn(dev, q)
        return dev.name

    async def stop(self, dev):
        q = self._queue(dev)
        async with q.lock:
            await self._halt(q)
            await dev.async_stop()
        return dev.name

    async def next(self, dev):
        q = self._queue(dev)
        async with q.lock:
            if not q.active:
                raise ValueError(f"Nothing roomcast started is playing in {dev.name}")
            following = q.following()
            if following is None:
                await self._halt(q)
                await dev.async_stop()
                return "nothing, that was the last one; stopped"
            await self._start(dev, q, q.index + 1)
        return following.label
