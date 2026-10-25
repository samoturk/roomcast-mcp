from unittest.mock import MagicMock

import pytest
from pychromecast.config import APP_MEDIA_RECEIVER

from roomcast.players import cast
from roomcast.players.base import MediaItem

SPOTIFY = "CC32E753"


class FakeCast:
    """A Chromecast whose media namespace answers the queue messages roomcast sends."""

    def __init__(self, app_id=APP_MEDIA_RECEIVER, state="PLAYING", ids=(1, 2), current=1,
                 other_state="IDLE", reply=None, media_namespace=True):
        self.name = "Nest Hub"
        self.uri = "10.0.0.9:8009"
        self.app_id = app_id
        self.app_display_name = "Spotify"
        self.state, self.ids, self.current, self.reply = state, list(ids), current, reply
        self.sent = []
        self.media_controller = MagicMock()
        # What pychromecast has cached right after connecting, whatever the app is doing.
        self.media_controller.status.player_state = "UNKNOWN"
        self.media_controller.is_active = media_namespace and app_id is not None
        self.other_state = other_state
        self.media_controller.send_message.side_effect = self._send
        self.start_app = MagicMock(side_effect=lambda app: setattr(self, "app_id", app))
        self.set_volume = MagicMock()

    @property
    def is_idle(self):
        return self.app_id is None

    def _send(self, message, inc_session_id, callback_function):
        self.sent.append(message)
        kind = message["type"]
        if self.reply:
            response = self.reply
        elif kind == "GET_STATUS" and self.app_id != APP_MEDIA_RECEIVER:
            response = {"type": "MEDIA_STATUS", "status": [{"playerState": self.other_state}]}
        elif kind == "GET_STATUS":
            status = [{"mediaSessionId": 7, "playerState": self.state, "currentItemId": self.current}]
            response = {"type": "MEDIA_STATUS", "status": status if self.state else []}
        elif kind == "QUEUE_GET_ITEM_IDS":
            response = {"type": "QUEUE_ITEM_IDS", "itemIds": self.ids}
        elif kind == "QUEUE_GET_ITEMS":
            response = {"type": "QUEUE_ITEMS", "items": [
                {"itemId": i, "media": {"metadata": {"title": f"t{i}", "subtitle": f"F{i}"}}}
                for i in message["itemIds"]]}
        else:
            response = {"type": "MEDIA_STATUS", "status": []}
        callback_function(True, response)

    def types(self):
        return [m["type"] for m in self.sent]

    def last(self, kind):
        return [m for m in self.sent if m["type"] == kind][-1]


BULLETINS = [MediaItem("https://n/1.mp3", "4am ET", "NPR", "audio/x-m4a"),
             MediaItem("https://t/1.mp3", "100 Sekunden", "Tagesschau")]


def test_load_takes_over_the_device_and_queues_everything_in_order():
    device = FakeCast(app_id=None)
    assert cast.load(device, BULLETINS, volume=40) == "Nest Hub"
    device.start_app.assert_called_once_with(APP_MEDIA_RECEIVER)
    device.set_volume.assert_called_once_with(0.4)
    items = device.last("QUEUE_LOAD")["items"]
    assert [i["media"]["contentId"] for i in items] == ["https://n/1.mp3", "https://t/1.mp3"]
    assert [i["media"]["contentType"] for i in items] == ["audio/mp4", "audio/mpeg"]
    assert items[0]["media"]["metadata"] == {"metadataType": 0, "title": "4am ET", "subtitle": "NPR"}


@pytest.mark.parametrize("declared,sent", [
    ("audio/mpeg", "audio/mpeg"), ("audio/x-mpeg", "audio/mpeg"), ("AUDIO/MP4", "audio/mp4"),
    ("video/mp4", "audio/mpeg"), (None, "audio/mpeg"),
    ("application/vnd.apple.mpegurl", "application/x-mpegURL"),
    ("application/x-mpegURL", "application/x-mpegURL"),
])
def test_content_type(declared, sent):
    assert cast.content_type(declared) == sent


@pytest.mark.parametrize("app_id,state,other_state,outcome,first", [
    (APP_MEDIA_RECEIVER, "PLAYING", "IDLE", "waiting", 3),
    (APP_MEDIA_RECEIVER, "PAUSED", "IDLE", "waiting", 3),
    (APP_MEDIA_RECEIVER, "IDLE", "IDLE", "started", 1),
    (SPOTIFY, None, "PLAYING", "busy", 0),
    (SPOTIFY, None, "BUFFERING", "busy", 0),
    (SPOTIFY, None, "PAUSED", "started", 1),
    (None, None, "IDLE", "started", 1),
])
def test_append_outcomes(app_id, state, other_state, outcome, first):
    device = FakeCast(app_id=app_id, state=state, other_state=other_state)
    assert cast.append(device, BULLETINS) == ("Nest Hub", first, outcome)
    if outcome == "waiting":
        insert = device.last("QUEUE_INSERT")
        assert insert["mediaSessionId"] == 7 and len(insert["items"]) == 2
        assert "QUEUE_LOAD" not in device.types()
    elif outcome == "started":
        assert len(device.last("QUEUE_LOAD")["items"]) == 2
    else:
        assert device.types() == ["GET_STATUS"]
        device.start_app.assert_not_called()


def test_describe_marks_the_current_item():
    assert cast.describe(FakeCast(current=2)) == "Queue in Nest Hub:\n1. F1: t1\n2. F2: t2 <- now playing"
    assert cast.describe(FakeCast(state="PAUSED")).endswith("t1 <- current (paused or stopped)\n2. F2: t2")
    assert cast.describe(FakeCast(state="IDLE")) == "The queue in Nest Hub is empty."
    assert cast.describe(FakeCast(app_id=None)) == "The queue in Nest Hub is empty."
    busy = cast.describe(FakeCast(app_id=SPOTIFY, other_state="PLAYING"))
    assert busy.startswith("Nest Hub is playing from Spotify")


def test_describe_does_not_launch_the_receiver_on_an_idle_device():
    device = FakeCast(app_id=None)
    cast.describe(device)
    assert device.sent == []
    device.start_app.assert_not_called()


def test_remove_and_jump_address_items_by_id():
    device = FakeCast(ids=(11, 12, 13))
    cast.remove(device, [3, 1, 1])
    assert device.last("QUEUE_REMOVE")["itemIds"] == [11, 13]
    with pytest.raises(ValueError, match="No queue position 4; the queue has 3 items"):
        cast.remove(device, [4])
    assert cast.jump(device, 2) == "Nest Hub"
    assert device.last("QUEUE_UPDATE") == {"type": "QUEUE_UPDATE", "mediaSessionId": 7,
                                           "currentItemId": 12}


def test_queue_ops_need_our_session():
    with pytest.raises(ValueError, match="No queue roomcast started is active in Nest Hub"):
        cast.jump(FakeCast(app_id=SPOTIFY, other_state="PLAYING"), 1)


def test_skip_moves_on_or_stops_after_the_last():
    device = FakeCast(ids=(1, 2), current=1)
    assert cast.skip(device) == "F2: t2"
    assert device.last("QUEUE_UPDATE")["currentItemId"] == 2
    last = FakeCast(ids=(1, 2), current=2)
    assert cast.skip(last) == "nothing, that was the last one; stopped"
    assert last.last("STOP")["mediaSessionId"] == 7


def test_refusals_surface_as_errors():
    device = FakeCast(app_id=None, reply={"type": "LOAD_FAILED", "reason": "INVALID_PARAMS"})
    with pytest.raises(RuntimeError, match="Nest Hub refused QUEUE_LOAD: INVALID_PARAMS"):
        cast.load(device, BULLETINS)


def test_an_app_without_the_media_namespace_is_not_asked_and_not_replaced():
    device = FakeCast(app_id="GAME", other_state="PLAYING", media_namespace=False)
    assert cast.other_app_playing(device) is True
    assert device.sent == []
    assert cast.append(device, BULLETINS) == ("Nest Hub", 0, "busy")
    device.start_app.assert_not_called()


def test_stop_asks_for_the_session_instead_of_trusting_the_cache():
    device = FakeCast(state="PLAYING")
    device.media_controller.status.media_session_id = None
    assert cast.stop(device) == "Nest Hub"
    assert device.types() == ["GET_STATUS", "STOP"]
    assert device.last("STOP")["mediaSessionId"] == 7


@pytest.mark.parametrize("app_id,state", [(None, None), (APP_MEDIA_RECEIVER, "IDLE")])
def test_stop_sends_nothing_when_nothing_plays(app_id, state):
    device = FakeCast(app_id=app_id, state=state)
    cast.stop(device)
    assert "STOP" not in device.types()


def test_hls_goes_out_as_a_live_hls_stream():
    item = cast.queue_item(MediaItem("https://8.8.8.8/live.m3u8", "Radio",
                                     media_type="application/vnd.apple.mpegurl"))
    assert (item["media"]["contentType"], item["media"]["streamType"]) == (
        "application/x-mpegURL", "LIVE")


@pytest.mark.parametrize("current,before,first", [(1, 2, 2), (3, None, 4), (99, None, 4)])
def test_next_inserts_before_the_item_after_the_current_one(current, before, first):
    device = FakeCast(ids=(1, 2, 3), current=current)
    assert cast.append(device, BULLETINS, after_current=True) == ("Nest Hub", first, "waiting")
    assert device.last("QUEUE_INSERT").get("insertBefore") == before


def test_long_lists_go_out_in_chunks():
    many = [MediaItem(f"https://n/{i}.mp3", f"t{i}") for i in range(45)]
    device = FakeCast(app_id=None)
    cast.load(device, many)
    assert device.types().count("QUEUE_LOAD") == 1
    inserts = [m for m in device.sent if m["type"] == "QUEUE_INSERT"]
    assert [len(m["items"]) for m in [device.last("QUEUE_LOAD"), *inserts]] == [20, 20, 5]
    assert all(m["mediaSessionId"] == 7 for m in inserts)
    playing = FakeCast(ids=(1, 2, 3), current=1)
    cast.append(playing, many, after_current=True)
    inserts = [m for m in playing.sent if m["type"] == "QUEUE_INSERT"]
    assert [len(m["items"]) for m in inserts] == [20, 20, 5]
    assert {m["insertBefore"] for m in inserts} == {2}
