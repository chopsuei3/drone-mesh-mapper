# City-wide tracking: remote XIAO nodes feeding one flightlog

> **Status: saved for future implementation, not started (2026-09-30).**
> Prerequisite: flash the pending dualcore firmware (NAN/boot-crash fixes and DJI
> DroneID) and deploy the matching flightlog files first. Decisions already taken:
> Tailscale, a Raspberry Pi relay at each site, and all four management features.

## Context

Today one XIAO sits on the home Pi's USB port and flightlog reads it directly. The goal
is a few more XIAOs at other locations around the city, all feeding the same flightlog
instance, so that coverage is wider and every drone and flight lives in one place.

**User's decisions:**
- **Firmware:** the XIAO firmware stays exactly as it is (the dualcore build, with the
  fixes the user is about to flash).
- **Relay host:** at each remote site a **Raspberry Pi** runs a small relay that reads
  the XIAO over USB and forwards to the server.
- **Network:** relays reach the home server over **Tailscale**. Nothing is exposed to the
  internet, and each node also gets its own token.
- **Management, all four options:**
  - offline and crash alerts
  - heard-by and coverage
  - a remote raw serial view
  - remote commands

**Working constraints:**
- The user deploys by copying files to the Pis and restarting services.
- Verification during development is local: automated tests plus headless
  screenshots. The real-hardware check is done on the Pis by the user.
- **This plan is set up now and executed later**: the pending firmware is flashed
  first.

### What already exists and is reused
- `ingest/serial_source.py`:
  - `open_serial_no_reset()` opens a port without rebooting the board.
  - The `SerialManager._reader` loop reconnects with back-off and sends
    `WATCHDOG_RESET` on connect.
  - A raw-line buffer and log feed the Sources page view.
  - `send()` writes commands to a port.
- `parse.LineParser` / `normalize()` are the single definition of what a firmware line
  means. Remote lines go through them unchanged.
- `Sessionizer.ingest(det, ts)` already takes an explicit timestamp. Identity by serial
  already merges a drone seen through different MACs, so one drone heard by two nodes is
  one drone.
- `services/notify.py` already handles channels, filters, cooldowns and delivery, and
  gains node events.
- `settings.py` and `/api/settings` hold new tunables.
- `RPI/install_flightlog.py` is the pattern for a relay installer: install in place,
  systemd, `dialout`, the ModemManager warning.

### Problems multi-node introduces (the reason for the sessionizer changes)
- **The same drone message is heard by two nodes.** The position and altitude are
  identical but the RSSI differs. The current duplicate rule
  (`flights.DUP_WINDOW_S`, keyed on lat/lon/alt/**rssi**) keeps both, which
  double-counts points.
- **Batches from different nodes arrive interleaved and slightly out of order.** The path
  would zig-zag between the two streams, inflating `path_len_m` many times over.
- **A node that was offline delivers a backlog later.** Those flights must land at their
  real times without firing "airborne now" alerts or geofence alerts.

## Design

### 1. Shared serial reader (refactor, no behaviour change)
- Extract `SerialManager._reader`'s port lifecycle into a `SerialReader` in a new
  `ingest/reader.py`: open without reset, `WATCHDOG_RESET`, reconnect/back-off, and a
  command write path. Its line handler is a callback,
  `on_line(port, line, t_monotonic)`.
- Extract the raw buffer and file log into a `RawLog` class keyed by *source* (`home/COM3`,
  `north/ttyACM0`), so remote lines share the Sources view.
- `SerialManager` becomes a thin user of both. The existing serial tests must pass
  unchanged.

### 2. Relay: `flightlog/relay.py`, run with `python -m flightlog.relay`
It lives in the same package as the server, but must never import Flask; it depends only
on `pyserial` and `requests`.
- **Ports.** It uses `SerialReader` on configured ports. `auto` picks Espressif USB
  devices (VID 0x303A).
- **Durable spool.** An SQLite file, `flightlog_relay.db`, holds
  `lines(seq, port, mono, boot, wall, line)`. Every line is spooled: detections, status
  lines, everything. Capped at 24 h or 200k lines; the oldest go first, and the relay
  counts what it dropped.
- **Sender.** Every 1 s, or as soon as 200 lines are waiting, it sends a gzipped
  `POST /api/ingest` with up to 500 lines. The server acks the highest `seq` it stored and
  the relay deletes up to that point. On failure it backs off exponentially to 60 s. It
  sends an empty batch every 10 s so the server can tell "relay alive, XIAO silent" from
  "relay gone".
- **Clock-proof timestamps.** Each line carries its **age** from the relay's monotonic
  clock, and the server stamps it `received_at - age`. This works even if a Pi without a
  hardware clock booted with the wrong time. Lines spooled before a relay restart carry
  their wall-clock time instead, clamped by the server.
- **Commands.** The ingest response can carry pending commands (STATUS,
  WATCHDOG_RESET). The relay writes them to the port and reports results in its next
  batch.
- **Relay status.** Each batch reports version, boot id, per-port connected state, spool
  depth, and dropped count.
- **`--replay FILE`** feeds a capture at real-time pace instead of serial. It's used for
  testing, and for trying a node without hardware.
- **`--status`** prints spool depth, last ack, ports and server reachability.

### 3. Server ingest
- **`POST /api/ingest`**
  - Auth: `Authorization: Bearer <node token>`. Tokens are 32 random bytes, stored as a
    sha256 hash and compared with `hmac.compare_digest`.
  - Responses: 401 for an unknown or revoked token, 403 for a disabled node, 413 over
    1 MB, 400 for a malformed body. It accepts `Content-Encoding: gzip`.
- **Idempotency.** A line with `seq` ≤ that node's `ack_seq` (same `spool_id`) is
  skipped, so a retried batch never duplicates data.
- **Per line:**
  1. Record it in `RawLog` under `node/port`, which gives the remote raw view.
  2. Update the node's last-line time. For a status line, store the parsed heartbeat
     (uptime, `reset`, counters) as the node's latest XIAO status.
  3. Run `LineParser` per `(node, port)`, then `sess.ingest(det, ts)`, with
     `det['rx_node']` set.
- **Local XIAO.** The home Pi's own XIAO becomes a built-in node named `home` (kind
  `local`), so every view treats all receivers alike.
- **Bindings.** `GET /api/ingest/hello` lets the installer check a token. The server
  already binds `0.0.0.0`, so it's reachable on its Tailscale address with no change.

### 4. Data model (`db.py`, via `ADDED_COLUMNS` and new tables)
- **`nodes`**:
  - identity: id, name (unique), kind (`local` | `relay`)
  - access: token_hash, token_hint (last 4), enabled
  - placement: lat, lon, notes, created_at
  - contact: last_contact_at, last_line_at, last_detection_at
  - status: xiao_status JSON, relay_status JSON, clock_offset_s
  - delivery: spool_id, ack_seq
- **`node_commands`**: id, node_id, port, command, created_at, delivered_at, result.
- **`detections.rx_node`** (INTEGER): the node that heard each stored point.
- **`flight_nodes`**: (flight_id, node_id, receptions, max_rssi, min_rssi, first_ts,
  last_ts), primary key (flight_id, node_id). It's upserted by the batched writer and is
  what "heard by" and coverage read. `edit.merge_flights` / `reassign` carry it along.

### 5. Multi-node rules in the sessionizer (`flights.py`)
- **A duplicate across nodes is a reception, not a point.** The same drone at the same
  lat/lon/alt within 2 s, from a *different* node, only updates `flight_nodes`. It stores
  no detection row, no track point and no stats. The existing same-node rule (1 s,
  including RSSI) is kept as-is, so a hovering drone still logs.
- **Out-of-order points count as presence only.** A point older than the flight's last
  applied point by more than 0.5 s updates `flight_nodes` and liveness, but not the path,
  speed, bounding box or altitude extremes. dt is never negative.
- **Backlog is ingested at its real time, but kept quiet.**
  - Detections older than 120 s never trigger takeoff alerts or geofence alerts.
  - Detections older than 60 s aren't pushed to the live map.
  - A backlog that doesn't overlap live data still forms normal past flights.
  - A backlog that overlaps another node's live tracking of the same drone contributes
    receptions only. That limitation is documented.
- IP-relayed detections are **not** marked `relayed`: that flag disables speed checks for
  LoRa, where timing is poor. Age-stamped relay timing is good.

### 6. Nodes page: `/nodes`, a new nav item
- **Table**:
  - name and status: *online*, *XIAO silent*, *relay offline*, *disabled*
  - relay version, uptime, ports and spool depth
  - the XIAO heartbeat: `reset` reason (highlighted if panic, watchdog or brownout),
    `queue_drops`, `dji` and the other counters
  - last detection and clock offset
- **Add node**: a name produces a token, shown **once**, with the exact installer
  command to paste on the remote Pi. Also: rotate token, disable, delete.
- **Location**: set by clicking the map. Nodes appear as markers.
- **Remote raw view**: the Sources raw view component with a source picker (`home/…`,
  `north/…`).
- **Commands**: STATUS / WATCHDOG_RESET buttons per port, showing *queued → delivered →
  result*.

### 7. Heard-by and coverage
- **Flight table**: a *Nodes* column listing who heard each flight, plus a node filter.
  `queries._filters` gains `rx_node` → `f.id IN (SELECT flight_id FROM flight_nodes …)`,
  so every existing view and export gets it.
- **Flight detail / drone page**: per-node receptions and best RSSI.
- **Maps**: node markers on the flight, live and analysis maps (a distinct shape, with
  popups for name and status).
- **Analysis**:
  - A node filter in the filter row.
  - A **Nodes** table: flights and drones heard, receptions, best RSSI.
  - **Typical and farthest range**: distance from the node's location to the drone
    positions it heard, p50/p95/max, computed in Python over a capped sample.
  - Optional coverage circles at the p95 range, to show where another node would help.

### 8. Offline and crash alerts (`services/notify.py`)
- Channels gain `events`: `takeoff` (default) and/or `nodes`. Existing channels are
  unchanged.
- A monitor thread, checking every 30 s, raises:
  - *relay offline*: no contact for `nodes.offline_after_s`, default 600
  - *XIAO silent*: relay online, but no line from the XIAO for that long
  - *node restarted*: heartbeat `reset` is panic, watchdog or brownout
  - *recovered*: the node is back

  Each event is sent once per state change, with a per-node cooldown.

### 9. Deployment and docs
- **`RPI/install_relay.py --server http://<tailscale-name>:5001 --token … --name …`**:
  1. checks `tailscale status`
  2. checks the token against `/api/ingest/hello`
  3. installs `pyserial` and `requests`
  4. writes `flightlog_relay.json`
  5. installs a `flightlog-relay` systemd unit with `dialout`, and warns about
     ModemManager
  6. starts it and prints `--status`
- **A remote Pi needs only** `flightlog/`, `RPI/install_relay.py` and
  `requirements.txt`. It doesn't need `static/` or Flask.
- **README**: Tailscale setup on the server and each relay; adding a node step by step;
  bandwidth (tiny once gzipped, fine on LTE); what backlog handling does and doesn't do.

## Files

- New:
  - `flightlog/relay.py`
  - `flightlog/ingest/reader.py` (`SerialReader`, `RawLog`)
  - `flightlog/nodes.py` (registry, tokens, status, commands, monitor)
  - `web/templates/nodes.html`, `web/static/js/nodes.js`
  - `RPI/install_relay.py`
- Modified:
  - `ingest/serial_source.py`, `app.py`, `db.py`, `flights.py`, `queries.py`,
    `analysis.py`, `edit.py`, `services/notify.py`, `settings.py`
  - `web/static/js/{map,flights,live,analysis,sources}.js`, `web/templates/base.html`
  - `README.md`
- **Firmware: none.**

## Milestones (each leaves the app working)
1. The `SerialReader` / `RawLog` refactor, with every existing suite green.
2. Ingest endpoint, nodes table, tokens, the local `home` node, `rx_node`,
   `flight_nodes`, the multi-node sessionizer rules and backlog quieting.
3. The relay: spool, sender, age timestamps, auto-detect, commands, replay, and the
   installer.
4. The Nodes page: status, add/rotate/disable, location, remote raw view, commands.
5. Heard-by and coverage across the flight table, maps and analysis.
6. Offline and crash alerts.
7. README and deploy notes.

## Verification (local)
- **Refactor**: all current suites pass unchanged (analysis, units, live, e2e,
  regression, DJI, firmware-logic).
- **Ingest**:
  - Missing, wrong or revoked token → 401; disabled node → 403; oversize → 413.
  - A resent batch stores nothing new. A gzip body works.
  - Remote lines parse exactly like local ones, using the real capture, status lines
    and DJI lines as fixtures.
- **Multi-node**:
  - Two replaying relays hear the same drone, interleaved and duplicated. Expect one
    flight, heard by both, with `path_len_m` equal to the single-node value (the
    double-count is fixed) and per-node max RSSI.
  - Out-of-order points leave the path unchanged.
  - Single-node behaviour is unchanged, including a hovering drone.
- **Outage**:
  - Stop the server for 2 min mid-replay. The spool grows; on restart the backlog is
    delivered exactly once, past flights have correct times, and no stale alert fires.
  - Restart a relay mid-spool. Prior-boot lines use wall time.
- **Clock**: a relay with its system clock 1 h wrong still produces correct timestamps
  (age-based).
- **Commands**: a fake serial port receives STATUS, and the result round-trips to the
  Nodes page.
- **Alerts**:
  - Stop a relay: *offline* after the threshold, then *recovered* when it's back.
  - A heartbeat with `reset: panic`: *restarted*. Delivered through the stub HTTP
    client.
- **End to end**: the server plus two relays in `--replay` mode against real HTTP.
  Headless screenshots of `/nodes`, the flight table's Nodes column, and analysis
  coverage.
- **On the Pis (user)**: Tailscale up on both; run `install_relay.py` on the remote Pi;
  the node shows *online*, and a test drone appears as *heard by* both nodes.

## Out of scope for this plan
- Estimating position from RSSI for drones that broadcast no GPS (multilateration).
  `flight_nodes` and node locations are the groundwork for it later.
- LoRa / Meshtastic transport. The ingest takes lines, so a LoRa bridge could feed it
  later.
- Login for the UI. Tailscale keeps it private; public exposure would need auth first.
