"""Turns what a caller passes (audio URLs, or one playlist URL) into items a speaker may play.

None of those URLs are roomcast's own: they come from feeds, web pages, or whatever the
model was shown. Roomcast sits on the home LAN, so it only ever follows public http(s)
addresses, and so do the speakers it hands URLs to."""
import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import replace
from posixpath import basename
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field

from roomcast.players.base import MediaItem

logger = logging.getLogger("roomcast.media")

MAX_ITEMS = 100
MAX_PLAYLIST_BYTES = 1_000_000
FETCH_SECONDS = 10
# Most entries of a podcast playlist share a few podcast CDNs and trackers; don't hit one with 100 at once.
RESOLVE_CONCURRENCY = 10
PLAYLIST_TYPES = {"audio/x-mpegurl", "audio/mpegurl", "application/x-mpegurl",
                  "application/vnd.apple.mpegurl"}
UNTYPED = {"", "text/plain", "application/octet-stream", "binary/octet-stream"}
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac")


class MediaInput(BaseModel):
    url: str = Field(max_length=4096)
    title: str | None = Field(default=None, max_length=300)
    subtitle: str | None = Field(default=None, max_length=300,
                                 description="Show, station or artist; shown before the title")


class MediaError(ValueError):
    """Shown to the caller as is, so it never carries anything read from a response."""


class UnsafeTarget(Exception):
    pass


NAT64 = ipaddress.ip_network("64:ff9b::/96")
NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """is_global, also for IPv6 addresses that carry an IPv4 one: through NAT64 or 6to4,
    64:ff9b::7f00:1 reaches 127.0.0.1, yet the stdlib calls it global."""
    if address.version == 6:
        if address.ipv4_mapped:
            return address.ipv4_mapped.is_global
        if address in NAT64:
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF).is_global
        if address in NAT64_LOCAL:
            return False
        if address.sixtofour:
            return address.sixtofour.is_global
    return address.is_global


async def is_public_http_url(url: str) -> bool:
    """http(s) to an address on the public internet, and nothing else.

    Without the check a playlist could point roomcast's GET, or the speaker's, at the
    router, or hand a Sonos a speaker-internal URI (x-file-cifs:, x-rincon-...)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        addresses = [ipaddress.ip_address(parts.hostname)]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                parts.hostname, port or (443 if parts.scheme == "https" else 80),
                type=socket.SOCK_STREAM)
        except OSError:
            return False
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    return bool(addresses) and all(_is_public(a) for a in addresses)


async def _guard(request: httpx.Request) -> None:
    if not await is_public_http_url(str(request.url)):
        raise UnsafeTarget(str(request.url))


def resolver_client(**kwargs) -> httpx.AsyncClient:
    """Checks every hop, not just the first: a public tracker can redirect anywhere."""
    return httpx.AsyncClient(follow_redirects=True, timeout=10,
                             event_hooks={"request": [_guard]}, **kwargs)


def _content_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";")[0].strip().lower()


async def resolve(client: httpx.AsyncClient, url: str) -> tuple[str, str | None] | None:
    """The final URL behind a redirect chain and the type it is served as, or None if it must
    not be played.

    Podcast enclosures sit behind chains of tracking redirects (NPR's goes through three
    hosts). Speakers cope with some and not others, so hand them the final URL. The deadline
    is overall, not httpx's per-phase one, so one trickling host cannot hold up the rest."""
    try:
        async with asyncio.timeout(FETCH_SECONDS), client.stream("GET", url) as response:
            kind = _content_type(response) if response.is_success else ""
            return str(response.url), (kind if kind not in UNTYPED else None)
    except UnsafeTarget as e:
        logger.warning("unsafe_media_url url=%s target=%s", url, e)
        return None
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, TimeoutError) as e:
        logger.warning("resolve_failed url=%s error=%r", url, e)
        return (url, None) if await is_public_http_url(url) else None


def title_from(url: str) -> str:
    try:
        parts = urlsplit(url)
        return unquote(basename(parts.path.rstrip("/"))) or parts.hostname or url
    except ValueError:
        return url


def parse_m3u(text: str, base_url: str) -> list[MediaItem]:
    """Entries of an M3U playlist, with titles from #EXTINF and the show from #EXTART.

    An HLS playlist (#EXT-X- tags) lists seconds-long segments of one stream, not tracks,
    so it is played as the one stream it is. Lines are split on CR/LF only: str.splitlines()
    would also split a title on U+2028 and friends, letting it smuggle in entries."""
    lines = re.split(r"\r?\n|\r", text)
    if any(line.strip().startswith("#EXT-X-") for line in lines):
        return [MediaItem(base_url, title_from(base_url))]
    items: list[MediaItem] = []
    title = artist = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            title = line.partition(",")[2].strip() or None
        elif line.startswith("#EXTART:"):
            artist = line.removeprefix("#EXTART:").strip() or None
        elif not line.startswith("#"):
            try:
                url = urljoin(base_url, line)
            except ValueError:
                title = artist = None
                continue
            if artist and title and title.startswith(f"{artist}: "):
                title = title.removeprefix(f"{artist}: ")
            items.append(MediaItem(url, title or title_from(url), subtitle=artist))
            title = artist = None
            if len(items) == MAX_ITEMS:
                break
    return items


async def _read_capped(response: httpx.Response) -> bytes:
    """Counts decoded bytes, so a compressed response cannot expand past the cap."""
    chunks, size = [], 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_PLAYLIST_BYTES:
            raise MediaError(f"The playlist is larger than {MAX_PLAYLIST_BYTES // 1_000_000}MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _looks_like_m3u(text: str) -> bool:
    first = next((line.strip() for line in re.split(r"[\r\n]", text) if line.strip()), "")
    return first.startswith(("#EXTM3U", "http://", "https://"))


async def read_playlist(client: httpx.AsyncClient, url: str) -> tuple[list[MediaItem], bool]:
    """A playlist URL's entries, or the URL itself when it turns out to be one audio file; the
    flag says the entries are already resolved (the audio case: its redirects were followed)."""
    try:
        # httpx's timeout is per phase; a server trickling bytes would never trip it.
        async with asyncio.timeout(FETCH_SECONDS), client.stream("GET", url) as response:
            if not response.is_success:
                raise MediaError(f"The playlist URL answered HTTP {response.status_code}")
            kind, final = _content_type(response), str(response.url)
            audio_by_name = kind in UNTYPED and urlsplit(final).path.lower().endswith(AUDIO_EXTENSIONS)
            if kind not in PLAYLIST_TYPES and (kind.startswith(("audio/", "video/")) or audio_by_name):
                return [MediaItem(final, title_from(final), media_type=None if audio_by_name else kind)], True
            if kind not in PLAYLIST_TYPES and kind not in UNTYPED:
                raise MediaError(f"That URL is neither a playlist nor audio (it is {kind})")
            text = (await _read_capped(response)).decode("utf-8", errors="replace")
    except TimeoutError:
        raise MediaError("The playlist URL took too long to answer") from None
    except UnsafeTarget:
        raise MediaError("The playlist URL leads to an address roomcast may not fetch") from None
    except MediaError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
        raise MediaError(f"Could not fetch the playlist ({type(e).__name__})") from None
    if kind not in PLAYLIST_TYPES and not _looks_like_m3u(text):
        raise MediaError("That URL is neither a playlist nor audio")
    return parse_m3u(text, final), False


async def gather_media(items: list[MediaInput] | None, playlist_url: str | None) -> list[MediaItem]:
    """What to play, each entry resolved to its final URL; unsafe entries are dropped."""
    if (items is None) == (playlist_url is None):
        raise MediaError("Pass either items or playlist_url, and not both")
    async with resolver_client() as client:
        if playlist_url is not None:
            entries, done = await read_playlist(client, playlist_url)
            if done:
                return entries
        else:
            entries = [MediaItem(i.url, i.title or title_from(i.url), i.subtitle) for i in items]
        limit = asyncio.Semaphore(RESOLVE_CONCURRENCY)

        async def bounded(url: str):
            async with limit:
                return await resolve(client, url)
        resolved = await asyncio.gather(*(bounded(e.url) for e in entries))
    return [replace(e, url=url, media_type=kind or e.media_type)
            for e, found in zip(entries, resolved, strict=True) if found
            for url, kind in [found]]
