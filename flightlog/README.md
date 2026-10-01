# flightlog

A flight-first rebuild of the mapper. The primary object is a **flight** — a
contiguous detection session for one drone — not a live marker on a map.

`mesh-mapper.py` is untouched and still runs. This lives alongside it and adds
no new dependencies beyond what that already needs.

## Run

```sh
python -m flightlog                         # http://127.0.0.1:5001
python -m flightlog --host 0.0.0.0          # reachable from other machines on the LAN
python -m flightlog --no-serial             # HTTP ingest only
python -m flightlog --retention-days 90     # age out raw points after 90 days
python -m flightlog --no-serial-log         # don't write flightlog_serial.log
```

Then open `/sources`, tick the node's port, and watch its **node** row — `alive`
means lines are arriving, even with no drone in range. There is no login: anyone
who can reach the port can relabel, merge or prune, so keep `--host 0.0.0.0` on a
network you trust.

The map opens on OpenStreetMap. Pick another base layer from the control at the
top right — satellite, dark, or an offline `tiles/` layer — and every page
remembers that choice in that browser.

Import existing history first, if you have any:

```sh
python -m flightlog.migrate                 # reads ./cumulative_detections.csv
python -m flightlog.migrate --csv other.csv --gap 90
```

The importer also pulls in `aliases.json`, `drone_tags.json` and
`faa_cache.csv`, and refuses to run twice without `--force`.

## On a Raspberry Pi

flightlog is a package, not a single file, so there is nothing to `wget` the way
`install_rpi.py` fetches `mesh-mapper.py`. Copy it across yourself, keeping the
layout — `RPI/` and `flightlog/` must stay siblings, and `static/` has to come
too or the map will be blank:

```
flightlog/          the application
static/             Leaflet + fonts, served at /vendor — required
requirements.txt
RPI/install_flightlog.py
mapper_test/        optional, generates test traffic
```

**Simplest: clone the fork on the Pi.** It already has the right layout:

```sh
git clone https://github.com/chopsuei3/drone-mesh-mapper.git ~/drone-mesh-mapper
```

Then skip straight to the install step below; later updates are a `git pull`
followed by `sudo systemctl restart flightlog`.

**Already copied the whole repo to the Pi?** Then there is nothing to move — `cd`
into that directory and skip straight to the install step below. The repo root
already has the right layout.

Otherwise, about 1.7 MB needs to travel. **Do not name the destination directory
`flightlog`** — the package inside it is called that, and it is easy to end up
with the contents flattened one level too high. From the machine holding this
checkout (Git Bash, or PowerShell — Windows ships OpenSSH):

```sh
cd /c/github/drone-mesh-mapper-reborn
ssh pi@raspberrypi.local 'mkdir -p ~/drone-mesh-mapper'
scp -r flightlog static requirements.txt RPI mapper_test pi@raspberrypi.local:~/drone-mesh-mapper/
```

Either way you want this on the Pi, with `flightlog/` as a *subdirectory* rather
than the top one:

```
~/drone-mesh-mapper/          <- run the installer from here
  flightlog/                  <- the package: __init__.py, app.py, web/ ...
  static/
  requirements.txt
  RPI/install_flightlog.py
  mapper_test/
```

Then on the Pi:

```sh
cd ~/drone-mesh-mapper
python3 RPI/install_flightlog.py --replace-legacy --import-legacy
```

That installs the dependencies, checks flightlog actually imports before changing
anything, removes the `mesh-mapper.py` `@reboot` cron entry so the USB port is
free, starts the `flightlog` systemd unit, and prints the LAN address plus the
serial devices it can see.

- Drop `--replace-legacy` to leave the crontab alone; the installer then only
  warns that the legacy app still owns the port. The crontab is backed up to
  `~/crontab.backup.*` before anything is removed.
- Drop `--import-legacy` if there is no `cumulative_detections.csv` to bring over.
- Removing the cron entry does not stop a *running* `mesh-mapper.py`. The
  installer spots one and prints the `kill` command; a reboot also does it.

A systemd unit rather than an `@reboot` cron job means it restarts on failure and
logs to journalctl. It is granted the `dialout` group so it can open
`/dev/ttyACM0` without changing your account.

```sh
sudo systemctl status flightlog       # is it running
journalctl -u flightlog -f            # what is it doing
sudo systemctl restart flightlog      # after copying new files over
python3 RPI/install_flightlog.py --uninstall
```

### Going back to mesh-mapper.py

It is untouched on disk (`~/mesh-mapper` by default). Stop flightlog first — only
one program can hold the serial port:

```sh
sudo systemctl stop flightlog
cd ~/mesh-mapper && python3 mesh-mapper.py
```

## Feeding it

Same wire format as before, so existing tooling works unchanged:

```sh
python mapper_test/mapper_test.py --host 127.0.0.1 --port 5001 --duration 5
```

**With notification channels set up, simulated drones send real alerts.** Turn
alerts off first — untick *send alerts* on the Sources page, or
`curl -X PATCH <app>/api/settings -H 'Content-Type: application/json' -d '{"notify.enabled": false}'`.

Serial ports are selected on the **Sources** page and reconnect automatically
when the device reappears. **A port can only be held by one program at a time** —
one reader per port is a physical constraint, not a convention (see the comment
at `mesh-mapper.py:13070`). While the legacy app holds the hardware, feed this
one over `POST /api/detections`.

The Sources page shows when each port last produced **any** line. The firmware
prints a status line once a minute even with nothing in range, so a connected node
quiet for well over that is flagged `silent` — otherwise a working node with no
drones nearby and a wedged one would look identical.

To see what the node is actually printing, use **Raw serial output** on the
Sources page: the most recent lines per source, newest at the bottom, with
heartbeats dimmed and the app's own writes to the device (`WATCHDOG_RESET`,
`STATUS`) marked. Sources are named node/port: this machine's XIAO is
`home/ttyACM0` (`home/COM3` on Windows), and a remote relay's is under its node's
name, e.g. `north/ttyACM0`. The same lines go to `flightlog_serial.log` in the install
directory, beside `flightlog.db` — capped at about 1 MB with two rotated backups
— so over SSH:

```sh
tail -f ~/drone-mesh-mapper/flightlog_serial.log
```

`--no-serial-log` turns the file off; the in-page view is always there.

## More receivers: relays at other sites

One XIAO hears a couple of kilometres. To cover more of a town, put more XIAOs at
other sites, each plugged into a Raspberry Pi running a small **relay**. Every relay
forwards what its XIAO prints to this flightlog over Tailscale, so every drone and
flight lands in one database, and a flight two sites hear is one flight, *heard by*
both.

```
site "north":  XIAO --USB-- Pi (flightlog relay) --+
                                                   +--Tailscale--> home Pi (flightlog) <--USB-- XIAO "home"
site "south":  XIAO --USB-- Pi (flightlog relay) --+
```

Nothing is exposed to the internet: relays reach the server over Tailscale, and each
has its own token. The XIAO firmware is the same everywhere.

### 1. Tailscale on the server

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale status                 # this machine's name, e.g. homepi
```

flightlog has to listen on every interface; the installed service already runs with
`--host 0.0.0.0`. From another device on your tailnet, `http://homepi:5001` should
open it. That name needs MagicDNS (on by default in the Tailscale admin console);
the machine's `100.x.y.z` address works too.

### 2. Add the node

On the **Nodes** page, set *relays reach this server at* to that address
(`http://homepi:5001`) and press Save. Then enter a name (`north`) and press
**Create**. The new node's panel shows its **token once**, inside the command to run
on the remote Pi:

```
python3 RPI/install_relay.py --server http://homepi:5001 --token flr_... --name north
```

Lost it? **New token** makes another, and the old one stops working at once.

### 3. The remote Pi

It needs two directories from this repo, `flightlog/` and `RPI/`. It does not need
`static/` or Flask. Copy them across, or clone the repo:

```sh
ssh pi@north-pi 'mkdir -p ~/drone-mesh-mapper'
scp -r flightlog RPI pi@north-pi:~/drone-mesh-mapper/
```

Then on that Pi:

```sh
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
cd ~/drone-mesh-mapper
python3 RPI/install_relay.py --server http://homepi:5001 --token flr_... --name north
```

The installer does the following, in order:

1. Checks that Tailscale is up.
2. Checks the token against the server. If it cannot get through, it stops before
   installing anything.
3. Installs `pyserial` and `requests`.
4. Writes `flightlog_relay.json`, readable only by you because it holds the token.
5. Installs and starts the `flightlog-relay` systemd service, with access to the
   serial port, and warns if ModemManager is running.
6. Prints the relay's status.

Plug the XIAO into that Pi. The relay finds it by its Espressif USB id (`--ports auto`,
the default); for a board behind a USB-serial bridge, name the port:
`--ports /dev/ttyACM0`. Within seconds the node reads **online** on the Nodes page.

```sh
sudo systemctl status flightlog-relay
journalctl -u flightlog-relay -f
python3 -m flightlog.relay --status          # spool, last ack, ports, server check
sudo systemctl restart flightlog-relay       # after copying new files over
python3 RPI/install_relay.py --uninstall     # keeps the config and the spool
```

### What the relay does

- **It keeps every line until the server has it.** Every line the XIAO prints goes
  into a small SQLite spool on the Pi (`flightlog_relay.db`). It is deleted only once
  the server confirms it stored it. A dropped link, a server restart or a Pi reboot
  loses nothing; the backlog goes out when the link is back. The spool holds 24 hours
  or 200,000 lines. Past that the oldest go, and the Nodes page shows how many.
- **It times lines by the Pi's monotonic clock, not its wall clock.** Each line
  carries its age, and the server subtracts that from its own clock. A Pi without a
  real-time clock that boots an hour wrong still places every detection correctly;
  the Nodes page shows its clock offset anyway. Only lines spooled before the relay
  restarted fall back to the Pi's wall clock.
- **A resent batch is never stored twice.** Every line has a sequence number, and the
  server skips any it already has.
- **It checks in every 10 s** even with nothing to send, so the server can tell
  "relay up, XIAO quiet" from "relay gone".
- **It can replay a capture.** `python3 -m flightlog.relay --replay capture.txt
  --server ... --token ...` feeds a plain file of serial lines, or a
  `flightlog_serial.log`, at its real pace instead of a XIAO. Use it to try a node
  without hardware.

### Bandwidth

Batches are gzipped JSON over a kept-alive connection:

- **Idle:** a check-in costs about 750 bytes with HTTP and WireGuard overhead. That is
  roughly 7 MB a day at the default 10 s, or 1.2 MB a day with `--heartbeat 60`
  (pass it to the installer, or set `"heartbeat_s"` in `flightlog_relay.json`).
- **A drone in range:** adds about 1 KB a second while it is heard.

That is fine on LTE. On a small data plan, raise the heartbeat. The server only calls
a relay offline after 10 minutes without contact.

### How several receivers count

- **One broadcast heard by two nodes is one point.** The same position and altitude
  from a second node within 2 s counts as a *reception*: the node is listed among those
  that heard the flight, but adds no point, distance or speed. A node's own
  back-to-back duplicates are still dropped by the rule above, which keys on RSSI, so a
  hovering drone still logs every second.
- **Points that arrive out of order count as presence.** Nodes send in batches, so
  their points interleave late. A fix more than half a second older than the newest one
  on the path keeps the flight alive and is stored. It does not move the path, speed,
  bounding box or altitude extremes, so the track never zig-zags between receivers.
- **A backlog is history, not news.** A relay's detections that arrive late are placed
  at the times they were heard, and kept quiet:
  - more than 60 s late, they never reach the live map;
  - more than 120 s late, they never send a takeoff alert or a geofence alert.

  Where nothing else was tracking the drone, they form ordinary flights. A backlog from
  before a drone's current live flight becomes a separate past flight.
- **Backlog limit:** where another receiver already recorded the flight, the late node
  is only added as a receiver. Its points do not extend that flight or move its start.
- **The home Pi's own XIAO is the node `home`.** Everything on the Nodes page applies to
  it too. Detections posted to `/api/detections` (mapper_test.py) name no receiver.

### The Nodes page

The table shows each receiver with:

- **status:** *online*, *XIAO silent*, *relay offline*, *waiting* (for first contact) or
  *disabled*;
- **the relay:** host, version, uptime, spool depth and clock offset;
- **the XIAO's last status line:** the `reset` reason (highlighted after a panic,
  watchdog or brownout), queue drops, DJI and the other counters;
- **the last detection.**

Select a node to:

- rename it or add notes;
- disable it (its batches are then refused);
- place it on the map;
- send STATUS or WATCHDOG_RESET to its port, and watch it go *queued → delivered →
  result*;
- rotate its token, or delete it;
- open its raw serial output.

**Commands get no answer from the dualcore XIAO.** Its firmware does not read serial
input: a command reaches its port (it shows as `TX>` in the raw view) and nothing comes
back. Node-mode home firmware answers STATUS.

### Heard by and coverage

- **Flight table:** the **Nodes** column lists who heard each flight, best signal
  first; hover for receptions and best RSSI. The *receiver* filter keeps only flights a
  node heard. It also narrows exports (`rx_node=`), and CSV exports gain a `heard_by`
  column.
- **Drone pages:** show every receiver that has heard that airframe.
- **Maps:** receivers with a location show as diamonds, coloured by status.
- **Analysis:** has a *heard by* filter and a **Receivers** table. For each node it
  shows flights, drones, receptions and best RSSI, plus its range: the distance from
  the node to the drone positions it heard. That is given as typical (median), far
  (95th percentile) and farthest, over its newest 20,000 positions.
- **Coverage** (on the Analysis map) draws each node's far range as a circle. Gaps
  between circles are where another node would help. A node needs a location for any of
  this.

### Node alerts

Tick **node problems** on a notification channel (Sources page) and it is told when:

- a relay has had no contact for the threshold (default 10 minutes, set on the Nodes
  page);
- a XIAO has printed nothing for that long;
- a status line shows a panic, watchdog or brownout restart;
- a node is back.

Each change alerts once. The same alert for the same node repeats on a channel at most
every 10 minutes, so a flapping link does not flood. Existing channels keep takeoff
alerts only.

| Endpoint | |
|---|---|
| `GET` / `POST /api/nodes` | list (status included, never tokens) / create - returns the token once |
| `GET` / `PATCH` / `DELETE /api/nodes/<id>` | read / `name`, `enabled`, `lat`, `lon`, `notes` / remove a relay |
| `POST /api/nodes/<id>/token` | a new token; the old one stops working |
| `GET` / `POST /api/nodes/<id>/commands` | recent / queue `{"port", "command": "STATUS" or "WATCHDOG_RESET"}` |
| `POST /api/ingest` | relay batches (`Authorization: Bearer <token>`, gzip allowed, 1 MB max) |
| `GET /api/ingest/hello` | checks a token: 200, 401 unknown or rotated, 403 disabled |
| `GET /api/analysis/nodes` | the Receivers table, with the Analysis filters |

Settings: `nodes.offline_after_s` (default 600) and `nodes.server_url` (what install
commands use), through `PATCH /api/settings`.

## Views

| Page | What it is |
|---|---|
| `/` | **Flight table** — sort, filter, multi-select rows onto the shared map, merge, export |
| `/live` | Everything currently airborne, pushed over SSE |
| `/drones` | Drone identity and groups — label a *drone*, not a MAC |
| `/drone/<id>` | One airframe's whole history, every flight on one map, MAC audit trail, FAA identification |
| `/analysis` | When, where, who and what — hour × weekday, launch and operator spots, receivers and their range, groups and drones, models and radio — all scoped by one filter row |
| `/nodes` | Every receiver — this machine's XIAO and each remote relay — with its health, location, commands, tokens and raw output |
| `/sources` | Serial ports, raw serial output from every receiver, notifications, geofences, database maintenance |

## Why identity is not MAC-keyed

RemoteID MACs are frequently randomized per power-cycle, so the legacy per-MAC
aliases silently stop following a drone. Here a drone record is resolved from
`basic_id` (the RemoteID serial) first, with MAC as a fallback, and every MAC it
has used is kept in `drone_macs` as an audit trail. A label attached to a drone
survives the rotation — and so does geofence enter/exit state, which the legacy
app keyed by MAC and so re-fired on every rotation.

Serials that cannot be trusted — too short, known placeholders like `NONE`, all
one character, longer than `ODID_ID_SIZE` — are rejected and the drone falls back
to MAC identity. Rejecting is the safe direction: accepting a junk serial would
silently merge every drone that broadcasts it into one record.

## What "flight" means

A contiguous **detection session**: a gap longer than `--gap` seconds (default
60, matching the legacy `staleThreshold`) ends a flight — **unless the drone comes
back within `--resume-window` seconds (default 180) broadcasting the same operator
position** (within 50 m). That is treated as an RF dropout, and the flight
continues.

The exception comes from real hardware. A DJI at -84 to -93 dBm was heard as
sparsely as one detection a minute, and the firmware's own heartbeat is also 60 s.
The capture carries no timestamps, but the heartbeat bounds them: with the lone
detection in one quiet minute arriving late in that minute — entirely plausible —
a plain 60 s gap splits that single flight into three, one of them a lone point,
and under-reports its distance by about a quarter (633 m of 854 m). The operator
position moved 0.2 m across the whole capture.

A drone that broadcasts no operator position gets the plain gap rule, and
`--resume-window 0` turns resuming off. The trade-off: two separate flights
launched from the same spot less than three minutes apart — a quick battery swap —
join into one. For anything the rules still get wrong, select the rows in the
table and hit **Merge**.

A flight with no GPS fixes at all is still recorded — RF presence is evidence —
and shows in the table as `no track`.

Altitudes are **MSL**, because that is what the firmware broadcasts
(`AltitudeGeo`). They are not height above ground.

## Data quality

Speed and heading are **derived** from successive fixes; the firmware decodes
them from ODID but never transmits them.

A step is rejected as a bad fix if it exceeds 10 km outright, or implies more
than 120 m/s when the timing is trustworthy. Rejected rows are still stored so
they stay inspectable, but contribute nothing to distance, bounding box,
altitude extremes or the drawn path, and are counted in `suspect_count` (shown
as a red badge in the table). The distance ceiling matters because detections
arrive in bursts — the home node flushes queued mesh packets back-to-back — so
judging purely on speed would reject entire good tracks.

Detections relayed over LoRa keep their distance contribution but report no
speed, rather than a fabricated one derived from arrival time.

Identical detections arriving back-to-back — same position, altitude and RSSI
within a second — are dropped. The firmware decodes RemoteID from both WiFi NAN
and beacon frames and emits every decode, so one reading can appear twice in a
row; counting both would inflate `Points` without adding anything. The same
reading a couple of seconds later is kept, because that is a hovering drone.

## Exports

`Export CSV` gives **summary rows with no path data** — duration, distance,
start/end coordinates, max altitude, speeds, RSSI. KML and GPX include paths.
All three stream, so a large export never builds up in memory. Every export
honours the filters currently applied to the table.

## Colours

Every drone gets a colour of its own, used for its swatch in the table and its
path on every map; all of one drone's flights share it. You can pick one on the
flight table (click the swatch) or the Drones page. Without a pick, a drone in a
group with a colour uses the group's; otherwise it gets an automatic colour that
steps the hue by the golden angle per drone, so drones seen one after another
come out about 137° apart. **Use default** on the Drones page clears a pick.

## Notifications

An alert goes out when a drone **takes off** — when a new flight opens. A flight
resuming after an RF dropout is the same flight, so it never alerts twice. A channel
can also take alerts about the receivers themselves; see *Node alerts* above.

The alert goes as soon as the flight has a GPS fix, or after `notify.settle_s`
(default 15 s) without one. If the drone's FAA lookup is still running when the
fix arrives, the alert waits for it — never past the settle time — so a new
drone's first alert can name its model. Each alert carries the label or serial,
FAA make/model, group and tag, whether the drone has been seen before, drone and
pilot positions as map links, altitude, signal (RSSI, band, channel), and a link
to the drone's page when `notify.base_url` is set.

### What each service needs

| Service | Required | Optional |
|---|---|---|
| Discord | `webhook_url` — in Discord: *Server Settings → Integrations → Webhooks → New Webhook*, pick the channel, *Copy Webhook URL* | `username` (who it posts as); `mention`: `@here`, `@everyone`, `<@&role_id>` or `<@user_id>` |
| Pushbullet | `access_token` — pushbullet.com: *Settings → Access Tokens → Create Access Token* | `device_iden` (one device — `GET https://api.pushbullet.com/v2/devices` lists them) **or** `channel_tag` (a Pushbullet channel you own). Default: all your devices |

Pushbullet's free tier allows 500 pushes a month, so keep a cooldown on it.

### Who gets alerted

Each channel has a `filter`:

- `{"mode": "any"}` — every drone. Add `"exclude_drone_ids": [12]` to leave out
  your own.
- `{"mode": "only", "drone_ids": [3, 7], "group_ids": [2], "tags": ["police"]}` —
  only drones that are listed, in a listed group, or carry a listed tag
  (untagged drones count as `unknown`).

The filter is checked when the alert is sent, so regrouping or retagging a drone
takes effect at once. `cooldown_s` (default 900) stops the same drone alerting
the same channel again within that time — a relaunch, or a flight split at the
edge of range. Skipped alerts are logged as `suppressed`; a failed delivery does
not start the cooldown.

### Setting it up

Use the **Notifications** card on the Sources page, or the API:

```sh
APP=http://raspberrypi.local:5001

# where alerts link back to
curl -X PATCH $APP/api/settings -H 'Content-Type: application/json' \
     -d '{"notify.base_url": "http://raspberrypi.local:5001"}'

# Discord: any drone except drone 12, pinging @here
curl -X POST $APP/api/notify/channels -H 'Content-Type: application/json' -d '{
  "type": "discord", "name": "ops",
  "config": {"webhook_url": "https://discord.com/api/webhooks/<id>/<token>", "mention": "@here"},
  "filter": {"mode": "any", "exclude_drone_ids": [12]}}'

# Pushbullet: only police-tagged drones and group 2, at most every 30 min per drone
curl -X POST $APP/api/notify/channels -H 'Content-Type: application/json' -d '{
  "type": "pushbullet", "name": "phone",
  "config": {"access_token": "o.<token>"},
  "filter": {"mode": "only", "tags": ["police"], "group_ids": [2]},
  "cooldown_s": 1800}'

curl -X POST $APP/api/notify/channels/1/test      # send a sample now
curl "$APP/api/notify/log?limit=20"               # what went out, what failed and why
```

Drone and group ids are in the drone pages' URLs, or from `GET /api/drones` and
`GET /api/groups`.

| Endpoint | |
|---|---|
| `GET` / `POST /api/notify/channels` | list (secrets masked) / create |
| `GET` / `PATCH` / `DELETE /api/notify/channels/<id>` | read / change — omit a secret to keep it / remove |
| `POST /api/notify/channels/<id>/test` | send a sample alert now; returns `{ok, detail}` |
| `GET /api/notify/log?limit=` | recent deliveries: `sent`, `failed` with the service's reason, `suppressed` |
| `GET` / `PATCH /api/settings` | `notify.enabled`, `notify.base_url`, `notify.settle_s`, `faa.auto` |

Secrets are masked whenever a channel is read back. Discord channels accept only
Discord webhook URLs and the Pushbullet endpoint is fixed, so unlike a geofence
webhook a channel cannot be aimed at an arbitrary host. Like the rest of the API
there is no authentication — anyone on the LAN can reach it.

## Identifying a drone

The first time a Remote ID serial flies, it is looked up in the FAA's **UAS
Declaration of Compliance** database (uasdoc.faa.gov) on a background thread,
spaced 2 s apart and never on the ingest path. The result shows in the
**Identification** card on the drone's page, beside its name in the flight table
and on the Drones page, and in alerts. Searching the table for a model name
works.

What it can tell you, and what it cannot:

- **Make, model, series, compliance category and DOC tracking number** — the
  *type* of aircraft, as its manufacturer declared it.
- **Not the owner.** FAA drone registrations are not public; only the FAA and law
  enforcement can link a Remote ID to a registrant.
- **Nothing from the FCC.** FCC records are keyed by FCC ID, which Remote ID does
  not broadcast.
- **Only serials the manufacturer filed.** The search is exact, not by prefix.

With no network at all, the drone page still decodes the serial itself: a
standard (ANSI/CTA-2063-A) serial is a 4-character manufacturer code, a length
character and the manufacturer's own number. A Remote ID that does not fit is
flagged — it is often a session ID rather than a serial.

### DJI DroneID

Current DJI aircraft broadcast standard Remote ID, which is decoded like any
other (every `1581F…` serial is a DJI). Older and Wi-Fi-linked DJI models
(Spark, Mavic Air, Mavic Mini and similar) instead put DJI's proprietary
DroneID in their Wi-Fi beacons; the dualcore firmware decodes that too and
tags the line `"id_type":"DJI"`. Such drones differ in three ways:

- **Their operator position is the takeoff point**, not a live pilot fix —
  DJI DroneID carries no pilot position. It is shown wherever a pilot position
  would be, but labelled *Home point* in alerts and on the drone's page, and
  the Analysis page's *Operator positions* notes it.
- **Their serial is DJI's own**, not a Remote ID serial, so it is never looked
  up in the FAA database.
- They carry a **DJI** badge in the flight table and drone lists, and the
  Analysis radio chart counts them as *DJI DroneID ch N*, apart from Remote ID.

DJI's OcuSync / O3 / O4 video-link DroneID is a different radio signal and
needs a software-defined radio; no ESP32 can receive it.

A match is kept (the drone page's **Look up again** refreshes it). A serial the
FAA does not know is retried at most daily, and a failed request after an hour,
each time the drone next flies. `faa.auto: false` — the tickbox on the Sources
page — stops automatic lookups; the drone-page button still works. Lookups need
the Pi to reach the internet.

## Analysis

One filter row — date range, group, drone, tag — scopes everything on the page,
and the view is kept in the URL so it can be bookmarked. Every view is built from
the same slice of flights, so the numbers always agree with each other.

- **When:** flight starts by hour and weekday (Pi local time) as one heatmap,
  with per-weekday and per-hour totals on its edges. *Show numbers* prints the
  counts in the cells.
- **Where:** launch points or operator positions, as circles sized by count. The
  grid gets finer as you zoom in, a spot straddling a grid line stays one
  circle, and clicking a circle lists the drones that used that spot.
- **Who:** groups, then drones, with flights, distinct days seen (regulars
  versus one-offs), airtime, a 24-hour strip of when each one flies, and its
  usual launch spot — click that to jump the map there. Clicking a row focuses
  the whole page on that group or drone.
- **What & how:** FAA make and model by number of drones, and how drones were
  heard — BLE or Wi-Fi, by channel. Detections from firmware that did not
  report band and channel show as *not reported*.

## Realtime

The live view takes one `/api/live` snapshot, then receives deltas over
Server-Sent Events at `/api/stream`. Position updates are **coalesced on a 4 Hz
tick**, so 60 detections/second produce 4 messages/second, not 60. There is no
poll loop. SSE rather than WebSockets because the traffic is one-way and it
needs no extra dependency — `flask-socketio` without `simple-websocket` silently
degrades to long-polling anyway.

## Offline maps

`.mbtiles` files in `tiles/` are served at `/tiles/<name>/{z}/{x}/{y}.<ext>` and
appear in the map's layer picker. This is the **same directory the legacy app
caches into**, so use its "Cache This Area" to populate it and both apps read
the same files. Vector tiles are served with the right `Content-Encoding`;
rendering them needs MapLibre, which is vendored but not yet wired here.

## Retention

Off by default. With `--retention-days N`, raw detection rows for closed flights
older than N days are deleted in chunks. **Flight summaries and cached paths are
kept forever** — the table, the map and exports are unaffected; only per-point
inspection of very old flights is lost. There is a Vacuum button on the Sources
page.

## Troubleshooting

- **Not sure the node is sending anything?** Watch **Raw serial output** on the
  Sources page, or `tail -f flightlog_serial.log`. A healthy node prints a status
  line about once a minute even with no drones about — on the current dualcore
  build it is `{"heartbeat":"dualcore active",...}` with radio counters (older
  builds print `{"   [+] Device is active and scanning..."}`) — and detections
  appear as JSON lines carrying `mac` and `drone_lat`.
- **Did the node crash?** The dualcore status line carries `uptime_s` and
  `reset`, the reason for the last restart. `panic` or a `watchdog` value means
  the firmware crashed; `power_on` or `external` is a normal start. `uptime_s`
  falling back towards zero between lines is a restart too.
- **Port is `connected` but the node row stays `no output yet` for over a minute,
  or shows `silent`.** Another program may hold the port, or the board is wedged.
  Unplug and replug it; a port that reappears reconnects on its own.
- **`Permission denied` on `/dev/ttyACM0` running by hand on Linux.** Your account
  needs the serial group: `sudo usermod -aG dialout $USER`, then log out and back
  in. The systemd service is already granted it.
- **The port keeps dropping on Ubuntu.** ModemManager probes new `/dev/ttyACM*`
  devices: `sudo systemctl disable --now ModemManager`. The installer warns if it
  is running.
- **Alerts are not arriving.** Press **Test** on the channel (Sources page): the
  result names the service's own reason — an invalid token, an unknown webhook.
  If the test works, check `GET /api/notify/log` — `suppressed` means the
  cooldown, and a drone missing from the log did not match the channel's filter
  — and that *send alerts* is ticked. Both services need the Pi online.
- **Flight table counts lag a few seconds behind a burst.** Rows are written in
  batches; the live view reads in-memory state and is never behind.
- **A node stays *waiting*.** On its Pi, run `python3 -m flightlog.relay --status`:
  it says whether the relay has run, how many lines are waiting, and whether the
  server answers. `journalctl -u flightlog-relay -f` shows each failed send and why.
- **A node shows *relay offline* but its Pi is up.** Check `tailscale status` on both
  machines, and that the server runs with `--host 0.0.0.0`. A `401` in the relay's log
  means its token was rotated or the node deleted: copy the new install command from
  the Nodes page and run it again.
- **A node shows *XIAO silent* while its relay checks in.** The XIAO is unplugged,
  unpowered or wedged; `--status` says whether its port is connected. Unplug and replug
  it.
- **A late node's flights look short.** Where another receiver had already recorded the
  flight, a backlog only adds receptions; see *How several receivers count*.
- **Dropped lines on the Nodes page.** The relay's spool overflowed: the link was down
  for over a day, or more than 200,000 lines piled up.

## Not carried over

- **In-app tile downloading.** Serving is here; caching new areas is still done
  from the legacy app, into the shared `tiles/` directory.
- **Vector-tile basemaps.** Served correctly, but the MapLibre bridge is not
  wired into the new map component yet.

## Layout

```
db.py         schema + connection (WAL, mirrors the proven MBTiles pooling)
settings.py   runtime settings kept in the database, changed over the API
colors.py     per-drone display colour (chosen > group > automatic)
parse.py      tolerant line -> detection; junk-serial filter
identity.py   basic_id/MAC -> durable drone record
flights.py    live sessionizer, batched writer, idle reaper
edit.py       merge / reassign / recompute (one definition of a flight's stats)
queries.py    every read query for the table and drill-downs
export.py     streaming CSV / KML / GPX
retention.py  chunked pruning + vacuum
bus.py        event bus + SSE, coalesced
migrate.py    legacy CSV + JSON state importer
nodes.py      receivers: registry, tokens, status, commands, monitor; relay batch ingest
relay.py      a remote site's relay (python -m flightlog.relay): spool + sender, no Flask
app.py        Flask routes  (python -m flightlog runs it)
ingest/       reader.py (SerialReader: DTR/RTS handling, one reader per port;
              RawLog: recent lines per source and flightlog_serial.log)
              serial_source.py (this machine's selected ports, parsed and ingested)
services/     tiles.py  geofence.py  faa.py (FAA identification)
              notify.py (Discord / Pushbullet takeoff and node alerts)
web/          templates + static (no build step, no npm)
```
