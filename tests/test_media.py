import asyncio
import socket
from unittest.mock import patch

import anyio
import httpx
import pytest

from roomcast import media
from roomcast.players.base import MediaItem

M3U = "audio/x-mpegurl; charset=utf-8"
real_client = media.resolver_client


def _serve(routes):
    """A transport answering by URL; a value is (content type, body) or a status code."""
    def handler(request):
        answer = routes.get(str(request.url))
        if answer is None:
            return httpx.Response(404)
        if isinstance(answer, int):
            return httpx.Response(answer)
        if isinstance(answer, httpx.Response):
            return answer
        kind, body = answer
        return httpx.Response(200, headers={"content-type": kind} if kind else {}, content=body)
    return httpx.MockTransport(handler)


def _gather(routes, items=None, playlist_url=None):
    with patch.object(media, "resolver_client", lambda: real_client(transport=_serve(routes))):
        return anyio.run(media.gather_media, items, playlist_url)


@pytest.mark.parametrize("url,ok", [
    ("https://8.8.8.8/a.mp3", True),
    ("http://192.168.178.1/admin", False),
    ("http://127.0.0.1:8765/x", False),
    ("http://[::1]/x", False),
    ("http://169.254.169.254/latest", False),
    ("http://10.0.0.5/x", False),
    ("ftp://8.8.8.8/a.mp3", False),
    ("x-file-cifs://nas/share/a.mp3", False),
    ("x-rincon-mp3radio://8.8.8.8/a", False),
    ("https:///nohost", False),
    ("https://8.8.8.8:99999/a.mp3", False),
    ("http://[bad/a.mp3", False),
    ("http://[64:ff9b::7f00:1]/x", False),
    ("http://[64:ff9b::c0a8:101]/x", False),
    ("http://[64:ff9b::808:808]/a.mp3", True),
    ("http://[64:ff9b:1::808:808]/x", False),
    ("http://[::ffff:127.0.0.1]/x", False),
    ("http://[2002:c0a8:101::1]/x", False),
])
def test_is_public_http_url(url, ok):
    assert anyio.run(media.is_public_http_url, url) is ok


def test_is_public_http_url_checks_resolved_addresses():
    async def private(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443))]

    async def check():
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", private):
            return await media.is_public_http_url("https://rebind.example/a.mp3")

    assert anyio.run(check) is False


def _resolve_with(handler, url):
    async def run():
        async with media.resolver_client(transport=httpx.MockTransport(handler)) as client:
            return await media.resolve(client, url)
    return anyio.run(run)


def test_resolve_follows_public_redirects():
    def handler(request):
        if request.url.host == "8.8.8.8":
            return httpx.Response(302, headers={"location": "https://1.1.1.1/final.mp3"})
        return httpx.Response(200, content=b"ID3")
    assert _resolve_with(handler, "https://8.8.8.8/track.mp3") == ("https://1.1.1.1/final.mp3", None)


def test_resolve_refuses_redirect_into_lan():
    hits = []

    def handler(request):
        hits.append(request.url.host)
        return httpx.Response(302, headers={"location": "http://192.168.178.1/reboot"})
    assert _resolve_with(handler, "https://8.8.8.8/track.mp3") is None
    assert hits == ["8.8.8.8"]


def test_resolve_network_error_keeps_only_public_original():
    def handler(request):
        raise httpx.ConnectError("down")
    assert _resolve_with(handler, "https://8.8.8.8/track.mp3") == ("https://8.8.8.8/track.mp3", None)


def test_m3u_titles_shows_and_relative_entries():
    text = ("#EXTM3U\n#EXTINF:-1,NPR: News Now\n#EXTART:NPR\nhttps://8.8.8.8/npr.mp3\n"
            "#EXTINF:123,Plain title, with a comma\nepisodes/2.mp3\n"
            "https://8.8.4.4/no-title%20here.mp3\n")
    assert media.parse_m3u(text, "https://1.1.1.1/lists/p.m3u") == [
        MediaItem("https://8.8.8.8/npr.mp3", "News Now", "NPR"),
        MediaItem("https://1.1.1.1/lists/episodes/2.mp3", "Plain title, with a comma"),
        MediaItem("https://8.8.4.4/no-title%20here.mp3", "no-title here.mp3"),
    ]


def test_m3u_is_capped():
    text = "\n".join(f"https://8.8.8.8/{i}.mp3" for i in range(150))
    assert len(media.parse_m3u(text, "https://8.8.8.8/p.m3u")) == media.MAX_ITEMS


def test_hls_is_one_stream():
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg1.ts\n#EXTINF:6.0,\nseg2.ts\n"
    assert media.parse_m3u(text, "https://8.8.8.8/live.m3u8") == [
        MediaItem("https://8.8.8.8/live.m3u8", "live.m3u8")]


def test_playlist_url_with_types_from_the_files_themselves():
    routes = {
        "https://1.1.1.1/p/abc": (M3U, b"#EXTM3U\n#EXTINF:-1,A: one\n#EXTART:A\nhttps://8.8.8.8/1.mp3\n"
                                         b"https://8.8.8.8/2\n"),
        "https://8.8.8.8/1.mp3": ("audio/mp4", b""),
        "https://8.8.8.8/2": ("application/octet-stream", b""),
    }
    assert _gather(routes, playlist_url="https://1.1.1.1/p/abc") == [
        MediaItem("https://8.8.8.8/1.mp3", "one", "A", "audio/mp4"),
        MediaItem("https://8.8.8.8/2", "2", None, None),
    ]


@pytest.mark.parametrize("kind,body", [
    ("text/plain", b"#EXTM3U\nhttps://8.8.8.8/1.mp3\n"),
    ("text/plain", b"https://8.8.8.8/1.mp3\n"),
    ("", b"#EXTM3U\nhttps://8.8.8.8/1.mp3\n"),
    ("application/vnd.apple.mpegurl", b"https://8.8.8.8/1.mp3\n"),
])
def test_playlists_are_recognised_by_type_or_content(kind, body):
    routes = {"https://1.1.1.1/list": (kind, body), "https://8.8.8.8/1.mp3": ("audio/mpeg", b"")}
    assert [m.url for m in _gather(routes, playlist_url="https://1.1.1.1/list")] == ["https://8.8.8.8/1.mp3"]


def test_an_audio_url_is_one_item_and_is_not_downloaded():
    def body():
        raise AssertionError("the audio body was read")
        yield b""

    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in body():
                yield chunk

    routes = {"https://8.8.8.8/ep.mp3": httpx.Response(200, headers={"content-type": "audio/mpeg"},
                                                       stream=Endless())}
    assert _gather(routes, playlist_url="https://8.8.8.8/ep.mp3") == [
        MediaItem("https://8.8.8.8/ep.mp3", "ep.mp3", None, "audio/mpeg")]


def test_an_untyped_file_with_an_audio_name_is_audio():
    routes = {"https://8.8.8.8/ep.MP3": ("application/octet-stream", b"ID3")}
    assert [m.title for m in _gather(routes, playlist_url="https://8.8.8.8/ep.MP3")] == ["ep.MP3"]


@pytest.mark.parametrize("routes,message", [
    ({"https://1.1.1.1/x": ("text/html", b"<html>")}, "neither a playlist nor audio (it is text/html)"),
    ({"https://1.1.1.1/x": ("text/plain", b"hello <secret>")}, "neither a playlist nor audio"),
    ({"https://1.1.1.1/x": 500}, "answered HTTP 500"),
    ({"https://1.1.1.1/x": ("audio/x-mpegurl", b"#" * (media.MAX_PLAYLIST_BYTES + 1))}, "larger than 1MB"),
])
def test_bad_playlists_are_refused_without_echoing_them(routes, message):
    with pytest.raises(media.MediaError) as e:
        _gather(routes, playlist_url="https://1.1.1.1/x")
    assert message in str(e.value) and "secret" not in str(e.value)


def test_a_playlist_redirecting_into_the_lan_is_refused():
    routes = {"https://1.1.1.1/p": httpx.Response(302, headers={"location": "http://192.168.1.1/p.m3u"})}
    with pytest.raises(media.MediaError, match="may not fetch"):
        _gather(routes, playlist_url="https://1.1.1.1/p")


def test_a_trickling_playlist_times_out():
    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b"#"

    routes = {"https://1.1.1.1/p": httpx.Response(200, headers={"content-type": M3U}, stream=Trickle())}
    with patch.object(media, "FETCH_SECONDS", 0.1), pytest.raises(media.MediaError, match="too long"):
        _gather(routes, playlist_url="https://1.1.1.1/p")


def test_items_and_playlist_url_are_exclusive():
    for items, url in ((None, None), ([media.MediaInput(url="https://8.8.8.8/a.mp3")], "https://8.8.8.8/p")):
        with pytest.raises(media.MediaError, match="either items or playlist_url"):
            _gather({}, items, url)


def test_items_get_titles_and_unsafe_ones_are_dropped():
    routes = {"https://8.8.8.8/a.mp3": ("audio/mpeg", b"")}
    items = [media.MediaInput(url="https://8.8.8.8/a.mp3"),
             media.MediaInput(url="http://192.168.1.5/nas.mp3", title="NAS"),
             media.MediaInput(url="https://8.8.8.8/b.mp3", title="B", subtitle="Show")]
    assert _gather(routes, items) == [
        MediaItem("https://8.8.8.8/a.mp3", "a.mp3", None, "audio/mpeg"),
        MediaItem("https://8.8.8.8/b.mp3", "B", "Show", None),
    ]


def test_unicode_line_breaks_inside_a_title_do_not_make_entries():
    text = "#EXTM3U\n#EXTINF:-1,News\u2028https://6.6.6.6/ad.mp3\x85x\nhttps://8.8.8.8/1.mp3\n"
    assert [m.url for m in media.parse_m3u(text, "https://1.1.1.1/p")] == ["https://8.8.8.8/1.mp3"]


def test_hls_tags_only_count_at_the_start_of_a_line():
    text = "#EXTM3U\n#EXTINF:-1,About #EXT-X-STREAM-INF tags\nhttps://8.8.8.8/1.mp3\n"
    assert [m.url for m in media.parse_m3u(text, "https://1.1.1.1/p")] == ["https://8.8.8.8/1.mp3"]


def test_a_trickling_entry_falls_back_to_its_unresolved_url():
    async def slow(request):
        await asyncio.sleep(10)
        return httpx.Response(200)

    async def run():
        async with real_client(transport=httpx.MockTransport(slow)) as client:
            return await media.resolve(client, "https://8.8.8.8/slow.mp3")
    with patch.object(media, "FETCH_SECONDS", 0.05):
        assert anyio.run(run) == ("https://8.8.8.8/slow.mp3", None)


def test_resolves_are_bounded_and_an_audio_playlist_url_is_fetched_once():
    in_flight = peak = 0
    hits = []

    async def handler(request):
        nonlocal in_flight, peak
        hits.append(str(request.url))
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        if request.url.path == "/list":
            body = "\n".join(f"https://8.8.8.8/{i}.mp3" for i in range(30)).encode()
            return httpx.Response(200, headers={"content-type": M3U}, content=body)
        return httpx.Response(200, headers={"content-type": "audio/mpeg"})

    def run(url):
        with patch.object(media, "resolver_client",
                          lambda: real_client(transport=httpx.MockTransport(handler))):
            return anyio.run(media.gather_media, None, url)
    assert len(run("https://1.1.1.1/list")) == 30
    assert peak == media.RESOLVE_CONCURRENCY
    hits.clear()
    assert len(run("https://8.8.8.8/ep.mp3")) == 1
    assert hits == ["https://8.8.8.8/ep.mp3"]


def test_a_malformed_entry_is_dropped_not_fatal():
    text = "#EXTM3U\nhttp://[bad\nhttps://8.8.8.8/1.mp3\n"
    assert [i.url for i in media.parse_m3u(text, "https://1.1.1.1/p.m3u")] == ["https://8.8.8.8/1.mp3"]
    routes = {"https://8.8.8.8/a.mp3": ("audio/mpeg", b"")}
    items = [media.MediaInput(url="https://8.8.8.8:99999/x.mp3"),
             media.MediaInput(url="https://8.8.8.8/a.mp3")]
    assert [i.url for i in _gather(routes, items)] == ["https://8.8.8.8/a.mp3"]


def test_a_malformed_playlist_url_is_a_clear_error():
    with pytest.raises(media.MediaError, match="address roomcast may not fetch"):
        _gather({}, playlist_url="https://8.8.8.8:99999/p.m3u")
