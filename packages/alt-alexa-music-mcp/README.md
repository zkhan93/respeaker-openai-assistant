# alt-alexa-music-mcp

A FastMCP server that exposes music playback as MCP tools. Backed by
Navidrome for the curated library and yt-dlp for on-demand fallback,
playback driven by a persistent `mpv` subprocess controlled via JSON
IPC.

Designed to run alongside the `voice-assistant` audio core on the Pi
(typically as a Docker container with the library volume bind-mounted)
and to be consumed by `alt-alexa` (the Realtime/MCP host) over LAN
streamable HTTP.

## Tools exposed

| Tool | Purpose |
|---|---|
| `play_music(query)` | Primary entry. Navidrome fuzzy match → stream, else yt-dlp + play. |
| `pause()` | Pause current playback. |
| `resume()` | Resume after pause. |
| `stop()` | Stop and clear current track. |
| `skip()` | v1 = stop. Queue lands in v2. |
| `now_playing()` | Title, artist, source, elapsed seconds, duration. |
| `set_volume(level)` | Music volume 0–100. Distinct from the duck channel. |
| `refresh_library()` | Trigger Navidrome `startScan` and wait for ack. |
| `list_library(query, limit)` | Browse the library (search-backed). |

## Run locally (dev)

```bash
cd packages/alt-alexa-music-mcp
cp config/config.toml.example config/config.toml
# edit config.toml: navidrome creds, library path
uv run alt-alexa-music-mcp
```

mpv and ffmpeg must be on `PATH`. On macOS: `brew install mpv ffmpeg`.
On Debian/Pi: `apt install mpv ffmpeg`.

## Run in Docker

The `Dockerfile` builds a slim image with mpv + ffmpeg + the package.
Library and the mpv socket are bind-mounted from the host so the
downloaded files persist and `alt-alexa` can poke the socket for
ducking.

```bash
docker build -t alt-alexa-music-mcp packages/alt-alexa-music-mcp

docker run -d --name alt-alexa-music-mcp \
  --network host \
  -v /tank-hdd/music:/library \
  -v /run/mpv:/run/mpv \
  -e MUSIC_MCP_NAVIDROME__BASE_URL=https://music.khancave.in \
  -e MUSIC_MCP_NAVIDROME__USERNAME=... \
  -e MUSIC_MCP_NAVIDROME__PASSWORD=... \
  alt-alexa-music-mcp
```

`--network host` is the simplest path for sharing PulseAudio /
PipeWire with the host. If you'd rather not, mount the audio socket
explicitly and skip `--network host`.

## Architecture (one-screen view)

```
┌─────────────────────────────────────────────────────────────┐
│ alt-alexa-music-mcp                                         │
│                                                             │
│  FastMCP HTTP  ─────────────────────────────────────────┐   │
│  (tools/*)                                              │   │
│      │                                                  │   │
│      ▼                                                  │   │
│  server.py ── play_music() ──┬──> navidrome.search3 ──┐ │   │
│                              │                        │ │   │
│                              │   rapidfuzz score      │ │   │
│                              │   >= threshold?        │ │   │
│                              │       ├── yes ─────────┼─┼──▶ player.loadfile(stream_url)
│                              │       └── no ──────────┘ │   │
│                              │                          │   │
│                              └──> yt.download() ────────┴──▶ navidrome.startScan() ──▶ search again ──▶ player.loadfile()
│                                   (async, returns                                                                       │
│                                    "downloading…" to caller)                                                            │
│                                                                                                                         │
│  player.py ── mpv subprocess ── JSON-IPC ──> /run/mpv/mpv.sock ◀── alt-alexa (duck/unduck)                              │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

`/run/mpv/mpv.sock` is intentionally a shared bind mount so the
consumer (`alt-alexa`) can hit `set property volume 25` directly on
hotword, bypassing MCP for low-latency ducking. The music server stays
authoritative for everything else.
