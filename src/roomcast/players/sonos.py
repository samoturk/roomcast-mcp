"""Sonos over local UPnP with SoCo. The speaker keeps the queue."""
import os
import threading

import anyio
import soco
from soco.data_structures import DidlMusicTrack, DidlResource

from roomcast.players.base import MediaItem, Outcome, check_positions, parse_hosts

# Discovery is SSDP multicast, which does not cross a Docker bridge network; a static map lets
# roomcast reach these rooms without it.
SONOS_HOSTS = parse_hosts(os.environ.get("ROOMCAST_SONOS_HOSTS", ""))


def speakers() -> dict[str, soco.SoCo]:
    """Rooms by name. A discovered zone replaces its ROOMCAST_SONOS_HOSTS entry, which only knows the
    name in lower case."""
    found = {name: (name, soco.SoCo(ip)) for name, ip in SONOS_HOSTS.items()}
    for zone in soco.discover(timeout=3) or ():
        found[zone.player_name.lower()] = (zone.player_name, zone)
    return dict(found.values())


# Sonos shows the file name for plain http(s) tracks and drops the DIDL title it was given,
# so roomcast remembers what each URI is to describe the queue. Lost on restart, when the
# queue falls back to Sonos's own titles.
_labels: dict[str, str] = {}
_labels_lock = threading.Lock()
MAX_LABELS = 1000


def _remember(uri: str, label: str) -> None:
    with _labels_lock:
        _labels.pop(uri, None)
        _labels[uri] = label
        while len(_labels) > MAX_LABELS:
            del _labels[next(iter(_labels))]


def _tracks(items: list[MediaItem]) -> list[DidlMusicTrack]:
    tracks = []
    for b in items:
        resource = DidlResource(uri=b.url,
                                protocol_info=f"http-get:*:{b.media_type or 'audio/mpeg'}:*")
        tracks.append(DidlMusicTrack(title=b.label, parent_id="", item_id="", resources=[resource]))
        _remember(b.url, b.label)
    return tracks


def _queue_is_active(speaker: soco.SoCo) -> bool:
    return speaker.get_current_media_info()["uri"].startswith("x-rincon-queue:")


def queue_and_play(zone: soco.SoCo, items: list[MediaItem], volume: int | None) -> str:
    speaker = zone.group.coordinator
    speaker.clear_queue()
    speaker.add_multiple_to_queue(_tracks(items))
    if volume is not None:
        speaker.group.volume = volume
    speaker.play_from_queue(0)
    return speaker.player_name


def append_to_queue(zone: soco.SoCo, items: list[MediaItem],
                    after_current: bool = False) -> tuple[str, int, Outcome]:
    """Adds to the queue and starts the new items only if the room is idle."""
    speaker = zone.group.coordinator
    state = speaker.get_current_transport_info()["current_transport_state"]
    if _queue_is_active(speaker):
        outcome = "started" if state == "STOPPED" else "waiting"
    else:
        outcome = "elsewhere" if state in ("PLAYING", "TRANSITIONING") else "started"
    current = 0
    if after_current and outcome == "waiting":
        current = int(speaker.get_current_track_info().get("playlist_position") or 0)
    if current:
        for offset, track in enumerate(_tracks(items)):
            speaker.add_to_queue(track, position=current + 1 + offset)
        return speaker.player_name, current + 1, outcome
    first = speaker.queue_size + 1
    speaker.add_multiple_to_queue(_tracks(items))
    if outcome == "started":
        speaker.play_from_queue(first - 1)
    return speaker.player_name, first, outcome


def describe_queue(speaker: soco.SoCo) -> str:
    queue = speaker.get_queue(max_items=100)
    if not queue:
        return f"The queue in {speaker.player_name} is empty."
    current = 0
    if _queue_is_active(speaker):
        current = int(speaker.get_current_track_info().get("playlist_position") or 0)
    playing = speaker.get_current_transport_info()["current_transport_state"] == "PLAYING"
    lines = []
    for position, item in enumerate(queue, 1):
        uri = item.resources[0].uri if item.resources else ""
        line = f"{position}. {_labels.get(uri) or item.title.split('?')[0]}"
        if position == current:
            line += " <- now playing" if playing else " <- current (paused or stopped)"
        lines.append(line)
    more = queue.total_matches - len(queue)
    if more > 0:
        lines.append(f"... and {more} more")
    return f"Queue in {speaker.player_name}:\n" + "\n".join(lines)


def remove_positions(zone: soco.SoCo, positions: list[int]) -> str:
    speaker = zone.group.coordinator
    check_positions(positions, speaker.queue_size)
    for position in sorted(set(positions), reverse=True):
        speaker.remove_from_queue(position - 1)
    return describe_queue(speaker)


def play_position(zone: soco.SoCo, position: int) -> str:
    speaker = zone.group.coordinator
    check_positions([position], speaker.queue_size)
    speaker.play_from_queue(position - 1)
    return speaker.player_name


def stop(zone: soco.SoCo) -> str:
    speaker = zone.group.coordinator
    speaker.stop()
    return speaker.player_name


def skip(zone: soco.SoCo) -> str:
    speaker = zone.group.coordinator
    current = int(speaker.get_current_track_info().get("playlist_position") or 0)
    if current >= speaker.queue_size:
        speaker.stop()
        return "nothing, that was the last one; stopped"
    speaker.next()
    track = speaker.get_current_track_info()
    return _labels.get(track.get("uri", "")) or track.get("title") or "next item"


class SonosPlayer:
    kind = "Sonos"

    async def rooms(self):
        return await anyio.to_thread.run_sync(speakers)

    async def play(self, room, items, volume):
        return await anyio.to_thread.run_sync(queue_and_play, room, items, volume)

    async def append(self, room, items, after_current=False):
        return await anyio.to_thread.run_sync(append_to_queue, room, items, after_current)

    async def queue(self, room):
        return await anyio.to_thread.run_sync(lambda: describe_queue(room.group.coordinator))

    async def remove(self, room, positions):
        return await anyio.to_thread.run_sync(remove_positions, room, positions)

    async def jump(self, room, position):
        return await anyio.to_thread.run_sync(play_position, room, position)

    async def stop(self, room):
        return await anyio.to_thread.run_sync(stop, room)

    async def next(self, room):
        return await anyio.to_thread.run_sync(skip, room)
