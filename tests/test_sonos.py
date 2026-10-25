from unittest.mock import MagicMock, patch

import pytest

from roomcast.players import sonos
from roomcast.players.base import MediaItem, parse_hosts

ITEMS = [
    {"subtitle": "NPR", "title": "4am ET", "url": "https://n/1.mp3", "media_type": "audio/mpeg"},
    {"subtitle": "Tagesschau", "title": "100 Sekunden", "url": "https://t/1.mp3", "media_type": None},
]


def test_parse_hosts():
    assert parse_hosts("Kitchen=10.0.0.2, Living Room = 10.0.0.3,,bad") == {
        "kitchen": "10.0.0.2", "living room": "10.0.0.3"}


def _zone(speaker):
    """A room as discovered; the calls act on its group's coordinator."""
    zone = MagicMock()
    zone.group.coordinator = speaker
    return zone


def test_queue_and_play_builds_queue_in_order():
    speaker = MagicMock(player_name="Kitchen")
    items = [MediaItem(**i) for i in ITEMS]
    zone = _zone(speaker)
    assert sonos.queue_and_play(zone, items, volume=40) == "Kitchen"

    speaker.clear_queue.assert_called_once()
    queued = speaker.add_multiple_to_queue.call_args.args[0]
    assert [q.title for q in queued] == ["NPR: 4am ET", "Tagesschau: 100 Sekunden"]
    assert queued[0].resources[0].uri == "https://n/1.mp3"
    assert queued[1].resources[0].protocol_info == "http-get:*:audio/mpeg:*"
    assert speaker.group.volume == 40
    speaker.play_from_queue.assert_called_once_with(0)


def test_speakers_merge_static_hosts_with_discovery_ignoring_case():
    kitchen = MagicMock(player_name="Kitchen")
    with patch.object(sonos, "SONOS_HOSTS", {"kitchen": "10.0.0.2", "garage": "10.0.0.3"}), \
         patch.object(sonos.soco, "SoCo") as SoCo, \
         patch.object(sonos.soco, "discover", return_value={kitchen}):
        found = sonos.speakers()
    assert set(found) == {"Kitchen", "garage"}
    assert found["Kitchen"] is kitchen
    assert found["garage"] is SoCo.return_value


def _queue_item(uri, title):
    return MagicMock(title=title, resources=[MagicMock(uri=uri)])


class _Queue(list):
    """soco's Queue: a list that also knows how many items the speaker holds in total."""
    @property
    def total_matches(self):
        return len(self)


def _speaker(queue=(), state="PLAYING", source="x-rincon-queue:RINCON_1#0", position="1"):
    speaker = MagicMock(player_name="Kitchen")
    speaker.queue_size = len(queue)
    speaker.get_queue.return_value = _Queue(queue)
    speaker.get_current_media_info.return_value = {"uri": source}
    speaker.get_current_transport_info.return_value = {"current_transport_state": state}
    speaker.get_current_track_info.return_value = {"playlist_position": position}
    return speaker


@pytest.mark.parametrize("state,source,outcome", [
    ("PLAYING", "x-rincon-queue:RINCON_1#0", "waiting"),
    ("PAUSED_PLAYBACK", "x-rincon-queue:RINCON_1#0", "waiting"),
    ("PLAYING", "x-sonos-htastream:RINCON_1:spdif", "elsewhere"),
    ("TRANSITIONING", "x-sonos-spotify:track", "elsewhere"),
    ("STOPPED", "x-rincon-queue:RINCON_1#0", "started"),
    ("PAUSED_PLAYBACK", "x-sonos-spotify:track", "started"),
])
def test_append_only_starts_an_idle_room(state, source, outcome):
    speaker = _speaker(queue=[_queue_item("u1", "a"), _queue_item("u2", "b")], state=state, source=source)
    items = [MediaItem(**i) for i in ITEMS]
    zone = _zone(speaker)
    assert sonos.append_to_queue(zone, items) == ("Kitchen", 3, outcome)
    speaker.clear_queue.assert_not_called()
    assert len(speaker.add_multiple_to_queue.call_args.args[0]) == 2
    if outcome == "started":
        speaker.play_from_queue.assert_called_once_with(2)
    else:
        speaker.play_from_queue.assert_not_called()


def test_describe_queue_uses_remembered_labels_and_marks_current():
    sonos._remember("https://n/1.mp3", "NPR: 4am ET")
    speaker = _speaker(queue=[_queue_item("https://n/1.mp3", "1.mp3"),
                              _queue_item("https://x", "x.mp3?listeningSessionID=1")],
                       position="2")
    assert sonos.describe_queue(speaker) == (
        "Queue in Kitchen:\n1. NPR: 4am ET\n2. x.mp3 <- now playing")
    assert sonos.describe_queue(_speaker(state="STOPPED")) == "The queue in Kitchen is empty."


def test_remove_positions_highest_first_and_validates():
    speaker = _speaker(queue=[_queue_item(f"u{i}", f"t{i}") for i in range(4)])
    zone = _zone(speaker)
    sonos.remove_positions(zone, [1, 3, 3])
    assert [c.args[0] for c in speaker.remove_from_queue.call_args_list] == [2, 0]
    with pytest.raises(ValueError, match="No queue position 5, 0; the queue has 4"):
        sonos.remove_positions(zone, [5, 0])


def test_play_position_bounds():
    speaker = _speaker(queue=[_queue_item("u", "t")])
    zone = _zone(speaker)
    assert sonos.play_position(zone, 1) == "Kitchen"
    with pytest.raises(ValueError):
        sonos.play_position(zone, 2)
    speaker.play_from_queue.assert_called_once_with(0)


def test_labels_are_bounded():
    with patch.object(sonos, "_labels", {}), patch.object(sonos, "MAX_LABELS", 2):
        for uri in ("a", "b", "c"):
            sonos._remember(uri, uri)
        assert list(sonos._labels) == ["b", "c"]


def test_skip_names_the_new_track_from_labels():
    sonos._remember("https://t/1.mp3", "Tagesschau: 100 Sekunden")
    speaker = _speaker(queue=[_queue_item("u0", "0.mp3"), _queue_item("https://t/1.mp3", "1.mp3")])
    speaker.get_current_track_info.side_effect = [
        {"playlist_position": "1"}, {"uri": "https://t/1.mp3", "title": "1.mp3"}]
    zone = _zone(speaker)
    assert sonos.skip(zone) == "Tagesschau: 100 Sekunden"
    speaker.next.assert_called_once()


def test_player_runs_soco_off_the_event_loop():
    import threading

    import anyio
    loop_thread = threading.get_ident()
    seen = []

    def fake_append(room, items, after_current):
        seen.append(threading.get_ident())
        return room, 1, "started"
    with patch.object(sonos, "append_to_queue", fake_append):
        assert anyio.run(sonos.SonosPlayer().append, "Kitchen", []) == ("Kitchen", 1, "started")
    assert seen and seen[0] != loop_thread


def test_next_inserts_after_the_current_track_one_by_one():
    speaker = _speaker(queue=[_queue_item(f"u{i}", f"t{i}") for i in range(4)], position="2")
    items = [MediaItem(**i) for i in ITEMS]
    assert sonos.append_to_queue(_zone(speaker), items, after_current=True) == ("Kitchen", 3, "waiting")
    assert [(c.args[0].title, c.kwargs["position"]) for c in speaker.add_to_queue.call_args_list] == [
        ("NPR: 4am ET", 3), ("Tagesschau: 100 Sekunden", 4)]
    speaker.add_multiple_to_queue.assert_not_called()
    speaker.play_from_queue.assert_not_called()


@pytest.mark.parametrize("state,source,outcome", [
    ("STOPPED", "x-rincon-queue:RINCON_1#0", "started"),
    ("PLAYING", "x-sonos-htastream:RINCON_1:spdif", "elsewhere"),
])
def test_next_behaves_like_end_unless_the_queue_is_playing(state, source, outcome):
    speaker = _speaker(queue=[_queue_item("u1", "a"), _queue_item("u2", "b")], state=state,
                       source=source, position="1")
    items = [MediaItem(**i) for i in ITEMS]
    assert sonos.append_to_queue(_zone(speaker), items, after_current=True) == ("Kitchen", 3, outcome)
    speaker.add_to_queue.assert_not_called()
    assert len(speaker.add_multiple_to_queue.call_args.args[0]) == 2


def test_skip_moves_on_or_stops_after_the_last():
    speaker = _speaker(queue=[_queue_item("u1", "a"), _queue_item("u2", "b")], position="1")
    sonos.skip(_zone(speaker))
    speaker.next.assert_called_once()
    last = _speaker(queue=[_queue_item("u1", "a"), _queue_item("u2", "b")], position="2")
    assert sonos.skip(_zone(last)) == "nothing, that was the last one; stopped"
    last.next.assert_not_called()
    last.stop.assert_called_once()
