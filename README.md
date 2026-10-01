# <div align="center">**Drone Mesh Mapper**</div>

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.7+-blue.svg)](https://www.python.org/)
[![ESP32](https://img.shields.io/badge/ESP32-Compatible-green.svg)](https://www.espressif.com/)
[![Flask](https://img.shields.io/badge/Flask-2.0+-red.svg)](https://flask.palletsprojects.com/)

**Real-time drone Remote ID detection · Meshtastic LoRa relay · live web map · fully offline-capable**

This fork adds **flightlog** — a flight-first logger with takeoff alerts, analysis and FAA
identification — and a **hardened dualcore firmware** that also decodes DJI DroneID.

[This fork](#this-fork) ·
[Quick Start](#quick-start) ·
[Features](#features) ·
[Offline Maps](#offline-maps) ·
[API Reference](#api-reference) ·
[Hardware](#hardware-setup)

<img src="eye.png" alt="Drone Detection Eye" style="width:50%; height:25%;">

</div>

---

## This fork

This is [chopsuei3/drone-mesh-mapper](https://github.com/chopsuei3/drone-mesh-mapper), a fork of
[colonelpanichacks/drone-mesh-mapper](https://github.com/colonelpanichacks/drone-mesh-mapper). It
keeps the original `mesh-mapper.py`, stays in sync with upstream, and adds two things:
**flightlog**, a different way to run the collection side, and a reworked **dualcore firmware**.

### flightlog — flights, not markers

The original mapper is a live map with a sidebar of MAC addresses.
[flightlog](flightlog/README.md) makes the *flight* the thing you work with: every detection
session for a drone, with its path, distance, duration, altitude and signal, stored in SQLite
and listed in a table you can sort, filter and export.

- **Flight table** as the home page. Filter by date, text (label, serial, MAC, model), group,
  tag, or whether a flight has a GPS track; put any selection of flights on one map; merge a
  flight that an RF dropout split in two; export CSV, KML or GPX.
- **Drones, not MACs.** A drone is identified by its Remote ID serial, so labels, tags, groups
  and colours follow it through MAC randomisation. Each drone gets its own colour automatically.
- **Live view** of everything airborne, pushed over Server-Sent Events.
- **Takeoff alerts** to Discord or Pushbullet — for any drone, or only chosen drones, groups or
  tags — with a per-drone cooldown. Configured on the Sources page or over the API.
- **FAA identification.** New serials are looked up in the FAA's UAS Declaration of Compliance
  database for make and model. Owner details are not public; the FAA shares them only with law
  enforcement.
- **Analysis**: an hour × weekday heatmap, launch points and operator positions on a map,
  per-drone and per-group breakdowns, the model mix, and how drones were heard (BLE, Wi-Fi
  channel, DJI DroneID) — all scoped by one filter row.
- **City-wide tracking**: XIAOs at other sites, each on a Raspberry Pi running a small relay,
  all feed one flightlog over Tailscale. A flight two sites hear is one flight, *heard by*
  both. The relay spools everything until the server has it, so outages lose nothing.
  A Nodes page shows every receiver's health and gives remote commands and raw output.
  Analysis shows each receiver's range and coverage, and channels can alert when a node
  goes offline or its XIAO crashes. Setup:
  [More receivers](flightlog/README.md#more-receivers-relays-at-other-sites).
- **Node health**: a raw serial view (every receiver's, local and remote), a rotating log file,
  and an alive/silent status for each port.
- Geofences with enter/exit webhooks, offline MBTiles basemaps (shared with `mesh-mapper.py`),
  optional retention, a Raspberry Pi installer that sets up a systemd service, and an importer
  for `mesh-mapper.py`'s CSV history.

flightlog serves on port 5001 and has **no login** — run it on your LAN or a private VPN only.

### Firmware — `remoteid-mesh-dualcore`

The XIAO ESP32-S3 dualcore build is reworked. Its output stays compatible with
`mesh-mapper.py`.

- **Crash fixes** inherited from upstream: a NULL-pointer write on the first Wi-Fi NAN Remote ID
  frame, a startup race that could reset the board when a drone was in range at boot, and
  unbounded parsing of beacon information elements.
- **No dropped detections** from the one-second stall in the mesh relay. The print queue is
  deeper, and one task owns the serial port, so lines never interleave.
- **Channel hopping** across 1–11 with extra time on 6 (the Wi-Fi NAN channel), instead of
  sitting on channel 6 and missing drones that beacon elsewhere.
- **BLE fix**: Remote ID is found anywhere in an advertisement, not only as its first record.
- **DJI DroneID** (Wi-Fi beacons, OUI `26:37:12`) is decoded for older and Wi-Fi-linked DJI
  models. Current DJI aircraft are already covered by standard Remote ID; DJI's OcuSync
  video-link DroneID needs a software-defined radio.
- **Every detection says how it was heard** (`band`, `channel`), and a once-a-minute status line
  carries radio counters and the reason for the last restart — `"reset":"panic"` or a watchdog
  value means the board crashed.

Build and flash it with PlatformIO, as described under [Firmware](#firmware). The other firmware
variants are unchanged from upstream.

### Known gaps

- The **Web Flasher and the prebuilt binaries in `firmware/` are upstream's**. They do not
  include the firmware changes above; build `remoteid-mesh-dualcore` from source.
- **`node-mode-dualcore`** still has the Wi-Fi NAN crash fixed above in the dualcore build.
- flightlog has **no authentication** (see above). Remote relays authenticate with a token
  each, but the web UI does not; keep it on your LAN or tailnet.
- The dualcore firmware **does not read serial input**. The Nodes page's STATUS and
  WATCHDOG_RESET reach the XIAO's port but get no reply.

### Changes so far

- **flightlog**: flight table, drone identity and groups, live view, geofences, exports, Pi
  installer, and import of `mesh-mapper.py` history.
- **Firmware coverage**: channel hopping, the BLE advertisement fix, `band`/`channel` on every
  detection, and the diagnostic status line.
- **flightlog**: raw serial view and log, remembered basemap, per-drone colours.
- **flightlog**: Discord/Pushbullet takeoff alerts, FAA identification, automatic per-drone
  colours.
- **flightlog**: the rebuilt Analysis page.
- **Firmware**: the crash and dropped-detection fixes, and DJI DroneID — with flightlog support
  for DJI drones (their home point is labelled as such, and they are never sent to the FAA
  lookup).
- **flightlog**: city-wide tracking — remote relays (`python -m flightlog.relay`,
  `RPI/install_relay.py`), the Nodes page, heard-by and coverage, and node alerts. The design
  is in [docs/plans/city-wide-tracking.md](docs/plans/city-wide-tracking.md).

---

## Overview

Captures FAA Remote ID broadcasts (BLE + WiFi) from drones using ESP32 nodes, relays detections over a Meshtastic LoRa mesh, and renders them in real time on a Leaflet web map. Optional FAA registration lookups, persistent multi-session tracking, KML/CSV/GeoJSON export, and **a fully self-contained offline mode** - UI, fonts, JS, and tiles all served from disk so you can pull the ethernet and still operate.

---

## Hardware Options

### Ready-to-Use Solution
Pre-built detection hardware designed specifically for this project, available at **[colonelpanic.tech](https://colonelpanic.tech)**:

- Complete kits with all components included
- Pre-flashed firmware ready to use
- Standalone mesh detection - no Pi or computer required
- Optional mapper integration for centralized monitoring

### DIY Build Option

| Component | Role |
|---|---|
| Xiao ESP32-S3 | Dual-core detection node (WiFi + BLE) |
| Heltec WiFi LoRa 32 V3 | Meshtastic relay |
| Wires | Three of them |

---

## Web Flasher

No PlatformIO required. Plug the board in, open the page in Chrome / Edge / Opera, hit Flash.

[colonelpanichacks.github.io/drone-mesh-mapper/flasher/](https://colonelpanichacks.github.io/drone-mesh-mapper/flasher/)

Three builds in one page:

- **Standard Dualcore** - single detector board, BLE + WiFi Remote-ID over UART to a Heltec V3
- **Node Mode / Remote** - field detector, tags each detection with a per-board `node_id`
- **Node Mode / Home** - base bridge, dedups multi-node hits by drone MAC, forwards to `mesh-mapper.py`

> **In this fork:** the flasher serves upstream's builds, which do not include this fork's
> dualcore fixes or DJI DroneID. Build `remoteid-mesh-dualcore` from source instead — see
> [Firmware](#firmware).

---

## Which app do I run?

Two apps live in this repo and share the firmware and the `static/` assets:

| | |
|---|---|
| **`flightlog/`** | **Recommended in this fork.** A flight-first rebuild — see [This fork](#this-fork) and **[flightlog/README.md](flightlog/README.md)**. |
| **`mesh-mapper.py`** | The original live map, kept in sync with upstream. One file, installed by `RPI/install_rpi.py`. The Features, ADS-B and API sections below document this one. |

Only one of them can hold a given USB port at a time. The flightlog installer can take over
the port and the autostart from `mesh-mapper.py` (`--replace-legacy`).

## Quick Start

### flightlog (Raspberry Pi)
```bash
git clone https://github.com/chopsuei3/drone-mesh-mapper.git ~/drone-mesh-mapper
cd ~/drone-mesh-mapper
python3 RPI/install_flightlog.py                 # add --replace-legacy to take over from mesh-mapper.py
```

The installer installs the dependencies, sets up and starts a `flightlog` systemd service on
port 5001, and prints the address to open. Then open **Sources**, tick the XIAO's serial port,
and wait for it to show `alive`. Details, upgrades and troubleshooting:
[flightlog/README.md](flightlog/README.md).

### flightlog relay (a XIAO at another site)
Add the node on flightlog's **Nodes** page; it shows a one-time install command. On the remote
Raspberry Pi, with Tailscale up on both machines and `flightlog/` and `RPI/` copied across:
```bash
python3 RPI/install_relay.py --server http://homepi:5001 --token flr_... --name north
```
Step by step: [More receivers](flightlog/README.md#more-receivers-relays-at-other-sites).

### mesh-mapper.py — automated (Raspberry Pi)
```bash
wget https://raw.githubusercontent.com/colonelpanichacks/drone-mesh-mapper/main/RPI/install_rpi.py
python3 install_rpi.py --branch main          # stable
python3 install_rpi.py --branch Dev           # latest
```

Optional flags: `--install-dir /opt/mesh-mapper`, `--no-cron`, `--force`.

### mesh-mapper.py — manual
```bash
git clone https://github.com/chopsuei3/drone-mesh-mapper
cd drone-mesh-mapper
pip3 install -r requirements.txt
python3 mesh-mapper.py
```

### CLI flags
| Flag | Default | What it does |
|---|---|---|
| `--web-port PORT` | 5000 | Port for the web UI |
| `--headless` | off | No web interface (server-only) |
| `--debug` | off | Verbose logging |
| `--port-interval SEC` | 10 | USB port re-scan cadence |
| `--no-auto-start` | off | Don't auto-connect to saved ports |

### Firmware
Pick the variant that matches your board. All build with PlatformIO:

| Path | Target | Notes |
|---|---|---|
| `node-mode-dualcore/` | ESP32-S3 dual-core | Remote node + home dedup node (`pio run -e remote` / `-e home`). Still has the Wi-Fi NAN crash. |
| `remoteid-mesh-dualcore/` | ESP32-S3 | BLE + WiFi concurrent detection, mesh relay. **Reworked in this fork** — crash fixes, channel hopping, DJI DroneID. Use this with flightlog. |
| `remoteid-mesh/` | ESP32-S3 / single-core | Original variant, GPIO6/7 pinout |
| `remoteid-c5-5g/` | ESP32-C5 | UNII-3 5GHz WiFi RID (channels 149/153/157/161/165) |

```bash
cd remoteid-mesh-dualcore
pio run -e seeed_xiao_esp32s3 -t upload
```

Name the environment: `remoteid-mesh-dualcore/platformio.ini` also defines a C6 target, and a
bare `pio run -t upload` builds and uploads both. In VS Code, pick
`env:seeed_xiao_esp32s3` → **Upload** in the PlatformIO sidebar.

---

## Features

> These are `mesh-mapper.py`'s features. flightlog's are under [This fork](#this-fork) and in
> [flightlog/README.md](flightlog/README.md).

### Real-time Mapping
- Live drone + pilot positions, broadcast rings, custom markers
- Flight-path tracking with persistent session state across restarts
- Multiple ESP32 receivers simultaneously
- Cyberpunk lime/magenta UI (Orbitron font, neon glow)

### Data Management
- Detection history with timestamps + RSSI
- Device aliases (friendly names per MAC)
- Export to CSV, KML (Google Earth), GeoJSON
- Cumulative long-term log

### ESP32 Integration
- USB serial auto-discovery + saved-port restore
- Real-time connection health
- Send diagnostic commands to connected nodes

### Web Interface
- WebSocket-driven live updates
- Mobile responsive
- Map / detection list / status panels in one view

### External
- FAA Remote ID registration lookup with 3-tier cache
- Webhook callbacks on detection transitions
- Service worker tile cache for the live UI

### Offline Maps
- 8 raster tile sources, vendored Leaflet + MapLibre GL
- One-click world baseline, region presets, place search
- Drop-in MBTiles import (raster or vector)
- Page loads with **zero internet** once tiles are cached

### ADS-B Air Traffic
- 6 sources: adsb.lol, adsb.fi, airplanes.live, OpenSky, ADSBexchange, plus **native Beast TCP** (HackRF / RTL-SDR / AirSpy / SDRplay via dump1090 / readsb / tar1090 / PiAware)
- Live aircraft markers, heading-rotated triangles, altitude-banded colors
- Aircraft trails per ICAO (60-point history)
- Click any aircraft for callsign / ICAO / altitude / speed / heading / vertical rate / squawk
- Polite to providers - bbox-only mode, configurable interval, exponential backoff on errors

---

## Offline Maps

The mapper is built to run with no internet. Everything the UI needs - Leaflet, MapLibre GL, Socket.IO, the Orbitron font - is vendored under `static/` and served off local disk, never a CDN. Map tiles live in `tiles/` as standard MBTiles files. The server serves them, the browser renders them, and you fly.

> **The tiles caveat.** `tiles/` is **empty on a fresh clone**, and the eight built-in basemaps listed below are online sources. So out of the box the interface is offline-capable but the basemap is not: with no connection and no cached `.mbtiles` you get a working map on a blank background - markers, tracks, pilot positions and geofences all draw correctly, because they come from your own detections rather than the basemap. **Cache your area while you still have a connection** and the map is genuinely self-contained in the field. The four ways to populate `tiles/` are below.

### How it works in 30 seconds

```
+------------------+    /tiles/<name>/{z}/{x}/{y}.png    +------------------+
|   Leaflet (UI)   | <----------------------------------- |  Flask backend   |
+------------------+                                      |  + SQLite reader |
         |                                                +--------+---------+
         | XYZ tile request                                        |
         |                                                         v
         |                                                 tiles/area.mbtiles
         |                                                 (one row per tile)
```

Tiles are stored in MBTiles format (SQLite, one row per `(z, x, y, blob)`). The Flask `/tiles/<name>/<z>/<x>/<y>.<ext>` route flips XYZ to TMS and serves bytes. PBF vector tiles get `Content-Encoding: gzip` set so MapLibre decodes them transparently.

### The 8 built-in raster sources

| Dropdown name | Best for | Server | Max zoom | Bulk-cache OK? |
|---|---|---|---|---|
| **Esri World Imagery** | Satellite / actual ground | server.arcgisonline.com | 19 | yes |
| **Esri World Topo** | Hillshade + roads | server.arcgisonline.com | 19 | yes |
| **Esri Dark Gray** | Minimal dark canvas | server.arcgisonline.com | 16 | yes |
| **CartoDB Dark Matter** | Cyberpunk dashboards (matches UI) | basemaps.cartocdn.com | 20 | yes |
| **CartoDB Positron** | Light minimal - drone tracks pop | basemaps.cartocdn.com | 20 | yes |
| **OSM Standard** | Classic streets reference | tile.openstreetmap.org | 19 | NO - TOS forbids |
| **OSM Humanitarian** | Amenities, water, terrain emphasized | tile.openstreetmap.fr | 20 | low volume only |
| **OpenTopoMap** | Backcountry / contours / trails | tile.opentopomap.org | 17 | low volume only |

Mind each provider's TOS yourself - **the cacher does not enforce it**. OpenStreetMap's main tile server forbids bulk download, but passing `--source osmStandard` with a wide bbox will still try. Use Esri/Carto for big jobs.

### Four ways to populate `tiles/`

#### 1. From the live map UI - Cache This Area

Open the **CACHE THIS AREA** panel in the sidebar:

```
PLACE SEARCH         type "Yosemite National Park", click result, bbox auto-fills
REGION PRESETS       pick from California / PNW / Continental US / 12 more
Source / Name / zMin / zMax    manual control
~ N tiles (~M MB)    live estimate while you adjust
START CACHE          kicks off a job, progress bar in sidebar
WORLD BASELINE       one-click globe overview at z0-6 (~80 MB) or z0-8 (~1.3 GB)
IMPORT MBTILES       paste URL or upload file
```

#### 2. Region Presets (one-click)

Built-in operational areas. Selecting one pans + auto-fills the cache name:

| Preset | bbox |
|---|---|
| California | [-125, 32, -114, 42] |
| Pacific Northwest (OR/WA) | [-125, 42, -117, 49] |
| Eastern Sierra | [-120, 37, -117, 40] |
| Continental US | [-125, 24.5, -66.9, 49.4] |
| New England | [-74, 40, -66, 47.5] |
| Appalachian Trail corridor | [-85, 30, -76, 39] |
| Florida / Texas / Hawaii / Alaska | ... |
| United Kingdom / Germany (west) / Japan | ... |

Add more by editing the `<select id="regionPreset">` block in `mesh-mapper.py`.

#### 3. From the CLI - `tools/cache_tiles.py`

```bash
# Single area, single source
python tools/cache_tiles.py \
    --bbox -122.6 37.6 -122.3 37.9 \
    --zoom 0 16 \
    --source esriWorldImagery \
    --out tiles/bay_area.mbtiles

# Globe baseline, every source (8 mbtiles files)
python tools/cache_tiles.py --preset world --source all --out tiles/

# Multi-source for one bbox (writes one mbtiles per source)
python tools/cache_tiles.py --preset world \
    --source esriWorldImagery,cartoDarkMatter,openTopoMap \
    --out tiles/

# Just count tiles, don't fetch
python tools/cache_tiles.py --preset world --source all --out tiles/ --dry-run
```

| Preset | bbox | zooms | tiles/source |
|---|---|---|---|
| `world` | global | 0-6 | ~5,500 |
| `world-z5` | global | 0-5 | ~1,400 |
| `world-z8` | global | 0-8 | ~88,000 |

The CLI is **resumable** - re-running skips tiles already in the MBTiles. Polite to free providers (50ms between fetches by default; tunable with `--rate`).

#### 4. Drop in a prebuilt file

Anything in standard MBTiles format works. Just copy it into `tiles/` and refresh.

| Source | Type | Notes |
|---|---|---|
| https://data.maptiler.com/downloads/ | raster + vector | Free tier with signup |
| https://openmaptiles.com/downloads/ | vector | Some free samples; commercial planet |
| Self-built with `tilemaker` | raster/vector | Geofabrik OSM extract to tiles |
| OpenMapTiles Docker pipeline | vector | https://github.com/openmaptiles/openmaptiles |

### Vector tile support (OpenMapTiles schema)

Drop a vector `.mbtiles` (format: pbf) in `tiles/` and it appears tagged **[V]** in the dropdown. Renders through MapLibre GL using the bundled cyberpunk style at [`static/styles/default-dark.json`](static/styles/default-dark.json).

```
[R] my_satellite (240 MB)    raster, served by Leaflet
[V] world_vector (52 MB)     vector, rendered by MapLibre GL
```

**Caveat**: the default style ships **without text labels** because glyph PBFs are bulky. Drop OpenMapTiles glyphs into `static/glyphs/<fontstack>/<range>.pbf` and add a `"glyphs"` key to the style JSON to enable place names.

### Recommended "hit the woods" loadout

Two raster mbtiles + one vector overview, totaling ~1 GB:

```bash
# 1. Globe-wide cyberpunk baseline (UI matches)
python tools/cache_tiles.py --preset world --source cartoDarkMatter --out tiles/

# 2. Satellite imagery for your AO
python tools/cache_tiles.py \
    --bbox -120.0 37.5 -119.0 38.5 \
    --zoom 8 16 \
    --source esriWorldImagery \
    --out tiles/op_zone_sat.mbtiles

# 3. Topo for terrain context
python tools/cache_tiles.py \
    --bbox -120.0 37.5 -119.0 38.5 \
    --zoom 8 14 \
    --source openTopoMap \
    --out tiles/op_zone_topo.mbtiles
```

Then pull the ethernet, refresh the page, switch the basemap dropdown - page renders entirely from disk.

### Place Search

Powered by [Nominatim](https://nominatim.openstreetmap.org/). Type a place name, get bbox results in the panel, click to apply. Respects Nominatim TOS (one req/sec max, meaningful User-Agent, results cached locally).

> Place search **requires internet** at search time. Cache the area first, then go offline. Region presets are 100% offline.

---

## ADS-B Air Traffic

Optional layer overlaying live aircraft on top of the drone RID feed (`mesh-mapper.py` only —
flightlog deliberately leaves ADS-B out and sticks to drones). Six sources, ranging from zero-setup to "I have a HackRF in the woods":

### Network sources (no setup, just internet)

| Source | Free | Key | Notes |
|---|---|---|---|
| **adsb.lol** | yes | no | Default; community-run, generous limits |
| **adsb.fi** | yes | no | Alternate provider, same JSON shape |
| **airplanes.live** | yes | no | Another community feed |
| **OpenSky Network** | yes | optional auth | Anonymous tier ~100 req/day, auth raises it |
| **ADS-B Exchange** | paid | RapidAPI key | Bring your own key |

### Local SDR sources (HackRF, RTL-SDR, AirSpy, SDRplay)

| Source | How |
|---|---|
| **Local SDR (JSON)** | Polls `dump1090` / `readsb` / `tar1090` / `PiAware` HTTP JSON. URL presets included for each common setup. Path of least resistance if you already have any running. |
| **Beast TCP (native)** | Direct TCP connect to a raw Mode-S Beast feed (default port 30005). Decodes in-process via [pyModeS](https://github.com/junzis/pyModeS). Requires `pip install pyModeS`. Eliminates the need for a separate web frontend. |

### Quick paths

```bash
# Easiest: pick adsb.lol in the dropdown, hit SAVE - done

# HackRF / RTL-SDR + dump1090 (JSON path)
sudo apt install dump1090-fa
# UI: pick "Local SDR · HackRF" preset → SAVE

# HackRF / RTL-SDR + Beast (native, no web frontend)
pip install pyModeS
dump1090-fa --net --net-bo-port 30005 --device-type hackrf
# UI: source = "Beast TCP raw feed", host=localhost, port=30005 → SAVE
```

### Behavior
- Heading-rotated triangle markers, altitude-banded colors (red <1k → violet 35k+ ft)
- Aircraft trails: 60-point polyline history per ICAO, color matches current altitude
- Stale aircraft (>60 sec since last seen) auto-evicted
- Polite: configurable poll interval (2-120 sec), bbox-restricted queries, exponential backoff on upstream errors
- Config persisted to `adsb_config.json`; auto-resumes on restart

---

## API Reference

> `mesh-mapper.py`'s API. flightlog's endpoints (flights, drones, analysis, notifications,
> settings) are documented in [flightlog/README.md](flightlog/README.md).

### Detections
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Main web interface |
| `GET` | `/api/detections` | Current active drone detections |
| `POST` | `/api/detections` | Submit new detection data |
| `GET` | `/api/detections_history` | Historical detection data (GeoJSON) |
| `GET` | `/api/paths` | Flight path data for visualization |
| `POST` | `/api/reactivate/<mac>` | Reactivate inactive drone detection |

### Device Management
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/aliases` | Get device aliases |
| `POST` | `/api/set_alias` | Set friendly name for device |
| `POST` | `/api/clear_alias/<mac>` | Remove device alias |
| `GET` | `/api/ports` | Available serial ports |
| `GET` | `/api/serial_status` | ESP32 connection status |
| `GET` | `/api/selected_ports` | Currently configured ports |

### FAA & Webhooks
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/faa/<identifier>` | FAA registration lookup |
| `POST` | `/api/query_faa` | Manual FAA query |
| `POST` | `/api/set_webhook_url` | Configure webhook endpoint |
| `GET` | `/api/get_webhook_url` | Get current webhook URL |
| `POST` | `/api/webhook_popup` | Webhook notification handler |

### ADS-B Air Traffic
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/adsb/sources` | List sources + dump1090 URL presets |
| `GET` | `/api/adsb/config` | Current config (credentials masked) |
| `POST` | `/api/adsb/config` | Update config (`enabled`, `source`, `interval`, `bbox`, source-specific fields) |
| `GET` | `/api/adsb/aircraft` | Current aircraft snapshot |

WebSocket event: `adsb` - pushed every poll cycle when enabled.

### Offline Tiles & Maps
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/tiles/<name>/<z>/<x>/<y>.<ext>` | Serve a tile from `<name>.mbtiles` (png/jpg/webp/pbf) |
| `GET` | `/styles/<name>.json` | Auto-generated MapLibre style JSON for a vector layer |
| `GET` | `/api/offline_layers` | List discovered MBTiles + format / kind / size / zoom range |
| `DELETE` | `/api/offline_layers/<name>` | Delete a cached layer |
| `POST` | `/api/cache_tiles` | Start a tile cache job (`{name, source, bbox, zmin, zmax}`) |
| `GET` | `/api/cache_jobs` | List all cache jobs |
| `GET` | `/api/cache_jobs/<id>` | Job progress + status |
| `POST` | `/api/cache_jobs/<id>/cancel` | Request cancel |
| `POST` | `/api/import_mbtiles` | Import via JSON `{name, url}` (download) or multipart upload |
| `GET` | `/api/import_jobs/<id>` | Import progress |
| `POST` | `/api/import_jobs/<id>/cancel` | Cancel import |
| `GET` | `/api/geocode?q=<query>` | Nominatim place search proxy (cached + rate-limited) |

### Data Export
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/download/csv` | Current detections (CSV) |
| `GET` | `/download/kml` | Current detections (KML) |
| `GET` | `/download/aliases` | Device aliases |
| `GET` | `/download/cumulative_detections.csv` | Full history (CSV) |
| `GET` | `/download/cumulative.kml` | Full history (KML) |

### System
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/diagnostics` | System health and performance |
| `POST` | `/api/debug_mode` | Toggle debug logging |
| `POST` | `/api/send_command` | Send command to ESP32 devices |
| `GET` / `POST` | `/select_ports` | Port selection interface |

### WebSocket Events
Pushed to connected clients in real time:
`detections`, `paths`, `serial_status`, `aliases`, `cumulative_log`, `faa_cache`

---

## Hardware Setup

### Supported ESP32 Boards
- **Xiao ESP32-S3** - Dual core, WiFi + BLE (recommended)
- **Xiao ESP32-C3** - Single core, WiFi only
- **Xiao ESP32-C5** - UNII-3 5GHz support
- **ESP32-DevKit** - Development & testing
- **Custom PCBs** - available at [colonelpanic.tech](https://colonelpanic.tech)

### Wiring for Mesh Integration
```
ESP32 Pin | Heltec V3 Pin   | Notes
----------|-----------------|------
TX1 (D4)  | RX 19           | Detection node -> mesh
RX1 (D5)  | TX 20           | Mesh -> home node
3.3V      | VCC             |
GND       | GND             |
```

> **`remoteid-mesh` variant uses GPIO6/7 instead** - check the firmware's `platformio.ini` before wiring.

### Heltec V3 Meshtastic Config (one-time)
```
serial.enabled  true
serial.mode     TEXTMSG
serial.baud     BAUD_115200
serial.rxd      19
serial.txd      20
```

---

## Performance

| Metric | Value |
|---|---|
| Detection latency | < 500ms |
| Concurrent drones | 50+ simultaneous |
| Memory (mapper) | < 100 MB typical |
| Per-detection storage | ~1 KB |
| Detections/min | 1000+ |
| Vendored UI assets | ~1.1 MB total (Leaflet + MapLibre + Socket.IO + Orbitron) |
| Tile cache rate | ~20 tiles/sec (50ms throttle, polite) |

---

## Troubleshooting

### ESP32 not detected
```bash
ls -la /dev/tty* | grep -E 'USB|ACM'
dmesg | grep tty
```
Hold the BOOT button while plugging in if the device shows up but won't program.

### Web interface won't load
```bash
netstat -tlnp | grep :5000     # is the server up?
tail -f mapper.log             # what's it saying?
```

### No drone detections
- Confirm firmware is flashed and running (`pio device monitor`)
- The dualcore build in this fork hops channels 1–11; upstream builds sit on channel 6, so
  they miss drones that beacon on other channels
- On this fork's dualcore build, read the once-a-minute status line: if `wifi_frames` and
  `ble_adv` keep climbing while `odid_wifi`, `odid_ble` and `dji` stay at 0, the radios
  work and nothing is broadcasting nearby
- Check that the Heltec is in serial mode at 115200, RX=19, TX=20
- Some drones don't broadcast Remote ID - required in many jurisdictions but not universal

### Tile cache job stuck
- Check `/api/cache_jobs/<id>` for `errors` count - likely upstream rate-limiting
- Bump `--rate` in `tools/cache_tiles.py` (e.g. `--rate 0.2` = 5 tiles/sec)
- OSM main server will silently throttle; use Esri/Carto for bulk

### Vector layer renders blank / weird
- Default style targets the OpenMapTiles schema; other schemas (Tilezen, Protomaps) need a custom style JSON
- Check the browser console - MapLibre logs unknown layer-source mismatches there
- Verify the mbtiles `metadata.format` is `pbf`

### Offline mode shows online tiles
- Pick a layer tagged `[R]` or `[V]` in the basemap dropdown - those are the offline ones
- The 8 named sources (Esri / Carto / OSM / etc.) are **online** layers; their dropdown labels do not have the `[R]`/`[V]` prefix

---

## Project Layout

```
drone-mesh-mapper/
|-- mesh-mapper.py              # Flask + SocketIO server, all UI inline
|-- flightlog/                  # This fork: the flight-first logger (see flightlog/README.md)
|-- docs/plans/                 # This fork: design notes for planned work
|-- requirements.txt
|-- static/                     # Vendored UI assets (offline-capable)
|   |-- leaflet/                # Leaflet 1.9.4
|   |-- maplibre/               # MapLibre GL 4.7.1 + leaflet plugin
|   |-- socketio/               # Socket.IO client
|   |-- fonts/                  # Orbitron TTF + @font-face CSS
|   `-- styles/                 # MapLibre vector styles
|-- tiles/                      # MBTiles files (auto-discovered)
|   `-- README.md               # Tile import / format notes
|-- tools/
|   `-- cache_tiles.py          # CLI tile pre-cacher
|-- RPI/                        # Raspberry Pi installers (install_rpi.py, install_flightlog.py)
|-- node-mode-dualcore/         # ESP32-S3 dual-role firmware
|-- remoteid-mesh-dualcore/     # ESP32-S3 BLE+WiFi firmware (reworked in this fork)
|-- remoteid-mesh/              # Single-core mesh firmware
|-- remoteid-c5-5g/             # ESP32-C5 5GHz firmware
`-- firmware/                   # Additional firmware variants
```

---

## Hardware Store

Get professional PCBs and complete kits at **[colonelpanic.tech](https://colonelpanic.tech)**.

---

## License

MIT - see [LICENSE](LICENSE).

## Acknowledgments

- **Cemaxecuter** / **alphafox02** - original RID firmware
- **Luke Switzer** - firmware contributions
- **OpenDroneID** community - protocol & specs (Apache 2.0)
- **Freek van Tienen** and **Jan Dumon** - the DJI DroneID decoding, as published in Kismet's
  `dot11_ie_221_dji_droneid` definition, which this fork's DJI parser follows
- **tsuinami-r1/drone-mesh-5plus** (MIT; no longer online) - a comparison with that fork surfaced
  several of the upstream firmware bugs fixed here
- **OpenStreetMap**, **Esri**, **CARTO**, **OpenTopoMap** - tile providers
- **MapLibre GL** + **Leaflet** + **Nominatim** - open mapping stack
- **ADS-B receivers** - built on the shoulders of [dump1090](https://github.com/MalcolmRobb/dump1090) (Malcolm Robb / mutability), [readsb](https://github.com/wiedehopf/readsb) + [tar1090](https://github.com/wiedehopf/tar1090) (wiedehopf), and [pyModeS](https://github.com/junzis/pyModeS) (junzis) for Mode-S/CPR decode. The Beast TCP path uses pyModeS directly; the JSON path is compatible with all of the above. Network sources: [adsb.lol](https://adsb.lol), [adsb.fi](https://adsb.fi), [airplanes.live](https://airplanes.live), [OpenSky](https://opensky-network.org), [ADSBexchange](https://adsbexchange.com).


<div align="center"><img src="boards.png" alt="boards" style="width:50%; height:25%;"></div>

---

<div align="center">

If this project helped you, give it a star.

</div>
