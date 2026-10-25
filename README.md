# roomcast-mcp

An MCP server that plays audio on the speakers in your home: Sonos, Google Cast (Chromecast,
Nest speakers and displays, Cast groups) and DLNA renderers. Give it audio URLs or one playlist
URL, and it plays them in the room you name, one after another, with a queue you can inspect
and edit.

Ask Claude "play the latest episode of that podcast in the kitchen", "put on a jazz station
in the living room" or "what's in the queue? drop the second one", and it happens on the real
speakers, from the Claude app on your phone or from any other MCP client.

```
MCP client (Claude app, Claude Code, ...)
    │  play_media(room="Kitchen", playlist_url=…)
    ▼
roomcast, on an always-on machine on your LAN
    │ fetches the playlist, follows each redirect chain (public addresses only)
    ├──UPnP (SoCo)────────────▶ Sonos
    ├──Cast v2 (pychromecast)─▶ Chromecast, Nest speakers and displays, Cast groups
    └──UPnP AV (async-upnp)───▶ other DLNA renderers (TVs, AV receivers, ...)
```

roomcast talks to the speakers directly on the LAN. It needs no speaker cloud account, no
developer registration and no credentials of any kind, and it does not look for audio itself:
it plays what it is given.

## Speakers
| Kind | Found by | Queue |
| --- | --- | --- |
| Sonos | SSDP, or `ROOMCAST_SONOS_HOSTS` | On the speaker, also visible in the Sonos app |
| Google Cast | mDNS, plus `ROOMCAST_CAST_HOSTS` | On the device, in the Default Media Receiver. Cannot be added to while another app (Spotify, YouTube Music) is playing there: `queue_media` then queues nothing and says so |
| DLNA renderer | SSDP, plus `ROOMCAST_DLNA_HOSTS` | **Held by roomcast**, which polls the renderer every 2 seconds and starts the next file when one ends. The renderer's own app never sees it, and it is lost when roomcast restarts |

`list_rooms` shows every speaker with its kind, and every speaker also answers to `kind:name`
(`cast:Kitchen`). Sonos speakers also announce themselves as DLNA renderers; the DLNA side
skips them. When two kinds share a name, the plain name goes to the one listed first in
`ROOMCAST_PLAYERS`.

I can only test on the speakers I own (a few Sonos and Nest devices). Other DLNA renderers and
Cast devices should work, but are best effort; reports and fixes are welcome.

## What it plays
- **A playlist URL**: M3U or M3U8, up to 100 entries and 1MB. Titles come from `#EXTINF` and
  the show or artist from `#EXTART`. An HLS playlist plays as the one stream it is. A URL that
  turns out to be a single audio file or stream plays as that.
- **Items**: up to 100 `{url, title?, subtitle?}`, for audio that is not in a playlist.

Every entry must be a **public** http(s) URL; see [Security](#security). It cannot play
Spotify, YouTube or Apple Music: they offer no direct audio URLs.

Things that work well:
- **Podcasts**: Claude reads a show's RSS feed, or a podcast tool hands it episode URLs.
- **Internet radio**: station stream URLs, from [Radio Browser](https://www.radio-browser.info)
  for example.
- **Another MCP server's playlist**: a tool that returns one playlist URL for "the news" or
  "my podcasts" makes a one-sentence briefing. roomcast's instructions tell the model to pass
  such a URL straight through instead of copying long item URLs.
- **Routines**: a scheduled agent run that starts the morning news in the bedroom.

## Tools
| Tool | Does |
| --- | --- |
| `play_media(room?, items? \| playlist_url?, volume?)` | Replaces the room's queue and plays |
| `queue_media(room?, items? \| playlist_url?, position="end" \| "next")` | Adds; starts only if the room is idle, never over TV, radio or another app. `"next"` inserts after the current item when the queue is playing, and otherwise behaves like `"end"` |
| `get_queue(room?)` | Numbered queue, current item marked |
| `remove_from_queue(positions, room?)` | Removes by position, returns the queue afterwards |
| `play_queue_item(position, room?)` | Jumps to a position |
| `skip(room?)` / `stop(room?)` | Skip / stop |
| `list_rooms()` | Every speaker, with its kind |

Sonos ignores the titles roomcast sends for plain http(s) files and shows the file name (or the
file's own ID3 tags) in its app. Playback is unaffected; `get_queue` still names each item from
what roomcast remembers. Cast and DLNA show the titles.

## Running it
roomcast has to run on a machine **on the same LAN as the speakers** that stays on: a home
server, a NAS, a Raspberry Pi (64-bit OS), a Mac mini.

### Docker (Linux)
Discovery is multicast (SSDP, mDNS), so the container uses host networking, which only works
with Docker on Linux. Images are published for `linux/amd64` and `linux/arm64`.

```bash
curl -O https://raw.githubusercontent.com/samoturk/roomcast-mcp/main/compose.yaml
curl -o .env https://raw.githubusercontent.com/samoturk/roomcast-mcp/main/.env.example
# edit .env, then:
docker compose up -d
docker compose logs -f roomcast
```

### Without Docker (Linux, macOS)
With [uv](https://docs.astral.sh/uv/):
```bash
ROOMCAST_DEFAULT_ROOM=Kitchen ROOMCAST_TOKEN=$(openssl rand -hex 32) \
    uvx roomcast-mcp
```
or `uv tool install roomcast-mcp` (or `pipx install roomcast-mcp`) for a permanent install. On macOS, allow "Local Network"
access for your terminal when asked, or discovery finds nothing.

### Connecting a client
Pick the setup that matches the client:

- **Claude Code, Cursor and other clients that can send a header**, on your LAN or tailnet:
  set `ROOMCAST_HOST=0.0.0.0` and `ROOMCAST_TOKEN`, then add
  ```bash
  claude mcp add --transport http roomcast http://<machine>:8765/mcp \
      --header "Authorization: Bearer <token>"
  ```
- **A client on the same machine** (Claude Desktop on the Mac mini that runs roomcast): run it
  over stdio, no network at all:
  ```json
  { "mcpServers": { "roomcast": { "command": "uvx",
      "args": ["roomcast-mcp", "--stdio"],
      "env": { "ROOMCAST_DEFAULT_ROOM": "Kitchen" } } } }
  ```
- **claude.ai and the Claude mobile app**: custom connectors connect from Anthropic's servers
  and cannot send a token, so roomcast needs a public HTTPS URL with a secret path. With
  [Tailscale Funnel](https://tailscale.com/kb/1223/funnel):
  1. Set `ROOMCAST_PATH_SECRET=$(openssl rand -hex 16)`.
  2. Either run `tailscale funnel --bg 8765` on the host, or use the sidecar: create a tagged
     auth key, set `TS_AUTHKEY`, and `docker compose --profile tailscale up -d`. Funnel has to
     be allowed for the node in the tailnet policy (`nodeAttrs` → `funnel`).
  3. Set `ROOMCAST_PUBLIC_HOST` to the `*.ts.net` name and restart; that turns on the
     Host-header check.
  4. claude.ai → Settings → Connectors → Add custom connector →
     `https://<host>.ts.net/<secret>/mcp`, no authentication. It then works in the phone app
     too.

  When you upgrade roomcast and its tools change, disconnect and reconnect the connector and
  start a new chat: claude.ai keeps the tool list it fetched when it connected.

## Configuration
All settings are environment variables (`.env` with Docker Compose).

| Variable | Default | |
| --- | --- | --- |
| `ROOMCAST_PLAYERS` | `sonos,cast,dlna` | Speaker kinds to look for, in order of precedence for shared names |
| `ROOMCAST_DEFAULT_ROOM` | | Room used when a request names none |
| `ROOMCAST_SONOS_HOSTS` | | `Name=IP,...`: Sonos rooms to reach without discovery |
| `ROOMCAST_CAST_HOSTS` | | Cast device IPs, polled as well as mDNS |
| `ROOMCAST_DLNA_HOSTS` | | DLNA renderer description URLs, used as well as SSDP |
| `ROOMCAST_HOST` | `127.0.0.1` | Listen address; `0.0.0.0` for the LAN |
| `ROOMCAST_PORT` | `8765` | |
| `ROOMCAST_TOKEN` | | Require `Authorization: Bearer <token>` on every request |
| `ROOMCAST_PATH_SECRET` | | Serve MCP at `/<secret>/mcp` instead of `/mcp` |
| `ROOMCAST_PUBLIC_HOST` | | Public hostname; requests for any other Host are refused |

## Security
Whoever can reach the endpoint can play, queue and stop audio on your speakers. They can make
roomcast fetch public URLs, and nothing else: it holds no credentials and reaches no other
service. So does a web page or a feed that talks your model into calling it.

- **Token or secret path.** With neither set roomcast logs a warning: anyone who reaches the
  port controls your speakers. With `ROOMCAST_PUBLIC_HOST` set it refuses to start without
  one. Prefer `ROOMCAST_TOKEN` wherever the client can send it. The secret path exists for
  chat connectors that cannot; treat that URL like a password.
- **Public addresses only.** Every URL is someone else's, so roomcast fetches and plays only
  public http(s) addresses: each redirect hop is checked, and anything resolving to a private,
  loopback or link-local address (including one wrapped in NAT64, 6to4 or IPv4-mapped IPv6),
  or using a Sonos-internal URI scheme, is refused or dropped. That also means media servers
  on your LAN (Jellyfin, Navidrome, a NAS) cannot be played yet.
- **What the check cannot cover.** It resolves each host before connecting, and the HTTP
  client resolves it again to connect: a hostile DNS server answering differently the second
  time (DNS rebinding) can still point roomcast's request at your LAN. The speakers also look
  up and fetch every URL themselves, which roomcast has no control over. What leaks back
  through roomcast is limited to lines of a response that look like playlist URLs.
- **Limits.** A playlist fetch is capped at 1MB and 10 seconds; at most 100 entries are taken.

## Development
```bash
uv sync --group dev
uv run pytest -q
uv run ruff check src tests
npx @modelcontextprotocol/inspector   # connect to http://localhost:8765/mcp
```

The image is Alpine-based (~120MB): every compiled dependency ships a musl wheel for both
architectures, and protobuf runs its pure-Python implementation.

## License
MIT
