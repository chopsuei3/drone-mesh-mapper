"""Flask app: flight-first API and views.

Runs on 5001 by default so it can sit alongside the legacy mesh-mapper.py on
5000 during the transition. Note the two apps cannot share a serial port - one
reader per port is a physical constraint, not a convention (see the comment at
mesh-mapper.py:13070). Feed this one over POST /api/detections while the old one
holds the hardware, or vice versa.

Remote XIAOs reach it through relays (relay.py) posting to /api/ingest; see
nodes.py.
"""
import argparse
import json
import logging
import os
import re
import time
import zlib

from flask import (Flask, jsonify, request, render_template, Response,
                   send_from_directory)

from . import queries
from . import analysis
from .db import Database
from .identity import IdentityResolver
from .flights import Sessionizer, DEFAULT_GAP_S, DEFAULT_RESUME_S
from .parse import normalize
from .export import flights_csv, flights_kml, flights_gpx
from .ingest.reader import RawLog, RAW_LOG_NAME, short_port, classify_line as _classify
from .ingest.serial_source import SerialManager, list_ports
from .nodes import (NodeRegistry, BatchIngest, MAX_BODY_BYTES, MAX_INFLATED_BYTES,
                    RELAY_KIND)
from . import edit
from .services import tiles as tiles_svc
from .services.geofence import GeofenceEngine
from .services.faa import FaaLookup
from .services.notify import Notifier
from .settings import Settings
from . import retention as retention_mod
from .bus import EventBus

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# How late a relayed detection may arrive and still count as live. A node
# delivering its backlog after an outage must place those flights in history
# without announcing them: past this, nothing reaches the live map...
LIVE_MAX_AGE_S = 60.0
# ...and past this, nothing alerts - no takeoff alert, no geofence crossing.
ALERT_MAX_AGE_S = 120.0


def _inflate(data, limit):
    """gunzip with a ceiling, so a small hostile body cannot expand without bound."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(data, limit + 1)
    if len(out) > limit or d.unconsumed_tail:
        raise OverflowError
    return out


def create_app(db_path=None, gap_s=DEFAULT_GAP_S, live=True, serial_enabled=True,
               retention_days=0, resume_s=DEFAULT_RESUME_S, serial_log=True):
    app = Flask(__name__,
                template_folder=os.path.join(os.path.dirname(__file__), 'web', 'templates'),
                static_folder=os.path.join(os.path.dirname(__file__), 'web', 'static'))
    # Nothing posted here is large; a relay batch is capped at MAX_BODY_BYTES
    # by its own route. This bounds a body sent without a Content-Length.
    app.config['MAX_CONTENT_LENGTH'] = 4 * MAX_BODY_BYTES

    db = Database(db_path)
    identity = IdentityResolver(db)
    sess = Sessionizer(db, identity, gap_s=gap_s, live=live, resume_s=resume_s)
    app.config['DB'] = db
    app.config['SESSIONIZER'] = sess

    bus = EventBus() if live else None
    app.config['BUS'] = bus

    retainer = retention_mod.RetentionJob(db, retention_days)
    if live:
        retainer.start()
    app.config['RETENTION'] = retainer

    settings = Settings(db)
    app.config['SETTINGS'] = settings

    faa = FaaLookup(db)
    app.config['FAA'] = faa
    # An alert due while the drone's FAA lookup is still running waits for it
    # (up to the settle time), so a new drone's alert can name its model.
    notifier = Notifier(db, settings, sess=sess, hold=faa.awaiting)
    app.config['NOTIFIER'] = notifier
    if live:
        # Only a live app looks drones up or sends alerts - never an import, a
        # test harness or a one-off script opening the same database.
        faa.start()
        notifier.start()
        if settings.get('faa.auto'):
            faa.sweep()             # drones mapped before lookups existed
    fences = GeofenceEngine(
        db, on_alert=(lambda p: bus.publish('geofence', p)) if bus else None)
    app.config['FENCES'] = fences

    # Fences evaluate on the ingest path, keyed by drone_id so a MAC rotation
    # inside a fence does not re-fire an enter alert.
    _drone_meta = {}

    def _meta(drone_id):
        m = _drone_meta.get(drone_id)
        if m is None:
            r = db.one("SELECT label, tag FROM drones WHERE id=?", (drone_id,))
            m = (r['label'] if r else None, (r['tag'] if r else None) or 'unknown')
            _drone_meta[drone_id] = m
        return m

    def _forget_drone(drone_id):
        """Drop cached label/tag so an edit takes effect on the next detection.

        Without this a tag change would not reach the geofence filter until the
        drone's current flight closed - which for a long flight could be hours.
        """
        _drone_meta.pop(drone_id, None)

    app.config['FORGET_DRONE'] = _forget_drone

    def _on_takeoff(payload, alert=True):
        """A new flight - not one resuming after a dropout. Queues the FAA lookup
        and the alert; both are queue pushes, and the slow work runs on their
        own threads, so this costs the ingest path next to nothing. A flight
        from a node's backlog is looked up but never alerted."""
        try:
            if settings.get('faa.auto'):
                faa.request(payload['drone_id'])
            if alert:
                notifier.flight_opened(payload)
        except Exception as e:
            logger.warning('takeoff hooks failed: %s', e)

    def _on_flight_event(name, payload):
        # How late the detection reached us: only relayed ones carry it.
        delay = payload.get('delay') or 0.0
        past = payload.get('past')
        if name != 'flight:point':
            if name == 'flight:open' and not payload.get('resumed'):
                _on_takeoff(payload, alert=not past and delay <= ALERT_MAX_AGE_S)
                if past or delay > LIVE_MAX_AGE_S:
                    return                  # history, not news: keep it off the live map
            if name == 'flight:close':
                _drone_meta.pop(payload.get('drone_id'), None)
            if bus is not None:
                bus.publish(name, payload)
            return
        lat, lon = payload.get('lat'), payload.get('lon')
        if lat is None or lon is None or past or delay > ALERT_MAX_AGE_S:
            return
        label, tag = _meta(payload['drone_id'])
        if bus is not None and delay <= LIVE_MAX_AGE_S:
            # Coalesced: only the newest position per flight survives each tick,
            # so 50 detections/s still produce 4 messages/s.
            bus.publish_position(dict(payload, label=label, tag=tag))
        try:
            fences.check(payload['drone_id'], lat, lon,
                         flight_id=payload.get('flight_id'), tag=tag, label=label)
        except Exception:
            pass
        try:
            notifier.flight_point(payload)      # a first fix sends a pending alert now
        except Exception as e:
            logger.warning('notify hook failed: %s', e)

    sess.on_event = _on_flight_event

    # Raw lines from every receiver - this machine's ports and every relay's -
    # for the raw views, and the size-capped file beside the database.
    log_path = None
    if live and serial_log and db.path != ':memory:':
        log_path = os.path.join(os.path.dirname(db.path), RAW_LOG_NAME)
    rawlog = RawLog(log_path)
    if rawlog.path:
        print("serial: raw output logged to " + rawlog.path)
    app.config['RAWLOG'] = rawlog

    nodes = NodeRegistry(db, settings)
    app.config['NODES'] = nodes
    home_id = nodes.ensure_local()
    batch_ingest = BatchIngest(nodes, sess, rawlog)
    if live:
        nodes.start()

    serial_mgr = None
    if serial_enabled:
        try:
            serial_mgr = SerialManager(
                sess, rawlog=rawlog,
                source_for=lambda port: '%s/%s' % (nodes.name(home_id), short_port(port)),
                on_line=lambda port, line, ts, det: nodes.line_seen(
                    home_id, port, ts, line, 'detection' if det is not None
                    else _classify(line)))
            serial_mgr.rx_node = home_id
            started = serial_mgr.autostart()
            if started:
                print("serial: reconnected to " + ", ".join(started))
        except Exception as e:                       # pyserial missing, no ports, etc.
            print("serial ingest unavailable: {0}".format(e))
            serial_mgr = None
    app.config['SERIAL'] = serial_mgr
    if serial_mgr is not None:
        nodes.local_send = serial_mgr.send
        nodes.local_ports = lambda: {p: {'connected': bool(serial_mgr.status.get(p))}
                                     for p in serial_mgr.selected}

    # Vendored libraries (leaflet, maplibre, socket.io, fonts) already live in
    # the repo's own static/ dir; serve them from there rather than duplicating.
    @app.route('/vendor/<path:filename>')
    def vendor(filename):
        return send_from_directory(os.path.join(BASE_DIR, 'static'), filename)

    # -- pages ---------------------------------------------------------------
    @app.route('/')
    def index():
        return render_template('flights.html')

    @app.route('/live')
    def live_page():
        return render_template('live.html')

    @app.route('/drones')
    def drones_page():
        return render_template('drones.html')

    @app.route('/analysis')
    def analysis_page():
        return render_template('analysis.html')

    @app.route('/sources')
    def sources_page():
        return render_template('sources.html')

    @app.route('/nodes')
    def nodes_page():
        return render_template('nodes.html')

    @app.route('/drone/<int:did>')
    def drone_page(did):
        return render_template('drone.html', drone_id=did)

    # -- ingest --------------------------------------------------------------
    @app.route('/api/detections', methods=['GET'])
    def get_detections():
        """Legacy-compatible liveness probe.

        mapper_test.py's test_connection() GETs this before it will send
        anything, so it has to exist and return 200. It reports currently-open
        flights keyed by MAC - roughly the old tracked_pairs shape - but is
        bounded by what is airborne rather than by all history.
        """
        out = {}
        for st in list(sess.open_flights.values()):
            out[st.mac] = {
                'mac': st.mac, 'flight_id': st.flight_id, 'drone_id': st.drone_id,
                'drone_lat': st.end_lat, 'drone_long': st.end_lon,
                'drone_altitude': st.max_alt, 'last_update': st.last_ts,
                'det_count': st.det_count, 'status': 'active'}
        return jsonify(out)

    @app.route('/api/detections', methods=['POST'])
    def post_detection():
        """Kept wire-compatible with the legacy endpoint so mapper_test.py works."""
        raw = request.get_json(silent=True)
        if not isinstance(raw, dict):
            return jsonify({'status': 'error', 'message': 'expected a JSON object'}), 400
        det = normalize(raw, source='http')
        if det is None or not det.get('mac'):
            return jsonify({'status': 'ignored'}), 200
        fid = sess.ingest(det)
        return jsonify({'status': 'ok', 'flight_id': fid}), 200

    # -- relay ingest -----------------------------------------------------------
    def _relay_node():
        """(node, None) for a valid bearer token, else (None, error response)."""
        auth = request.headers.get('Authorization') or ''
        token = auth[7:].strip() if auth[:7].lower() == 'bearer ' else None
        node = nodes.authenticate(token)
        if node is None:
            return None, (jsonify({'error': 'unknown or revoked token'}), 401)
        if not node['enabled']:
            return None, (jsonify({'error': 'node %s is disabled' % node['name']}), 403)
        return node, None

    @app.route('/api/ingest', methods=['POST'])
    def api_ingest():
        """One batch from a relay. See relay.py for the sender, nodes.py for
        what happens to each line. Lines at or below the node's acknowledged
        sequence number are skipped, so a resent batch stores nothing twice."""
        node, err = _relay_node()
        if err:
            return err
        if (request.content_length or 0) > MAX_BODY_BYTES:
            return jsonify({'error': 'batch over %d bytes' % MAX_BODY_BYTES}), 413
        data = request.get_data(cache=False)
        if len(data) > MAX_BODY_BYTES:
            return jsonify({'error': 'batch over %d bytes' % MAX_BODY_BYTES}), 413
        if (request.headers.get('Content-Encoding') or '').lower() == 'gzip':
            try:
                data = _inflate(data, MAX_INFLATED_BYTES)
            except OverflowError:
                return jsonify({'error': 'batch inflates past %d bytes' % MAX_INFLATED_BYTES}), 413
            except zlib.error:
                return jsonify({'error': 'body is not valid gzip'}), 400
        try:
            body = json.loads(data.decode('utf-8'))
            BatchIngest.validate(body)
        except (ValueError, UnicodeDecodeError) as e:
            return jsonify({'error': 'malformed batch: %s' % e}), 400
        return jsonify(batch_ingest.handle(node, body, time.time()))

    @app.route('/api/ingest/hello')
    def api_ingest_hello():
        """Lets the relay installer check its server address and token."""
        node, err = _relay_node()
        if err:
            return err
        return jsonify({'node': node['name'], 'id': node['id'], 'server_time': time.time(),
                        'spool_id': node['spool_id'], 'ack_seq': node['ack_seq']})

    # -- nodes --------------------------------------------------------------------
    def _install_command(name, token):
        base = settings.get('nodes.server_url') or request.host_url.rstrip('/')
        return ('python3 RPI/install_relay.py --server %s --token %s --name %s'
                % (base, token, name))

    @app.route('/api/nodes')
    def api_nodes():
        return jsonify(nodes.list())

    @app.route('/api/nodes', methods=['POST'])
    def api_node_create():
        try:
            node, token = nodes.create(request.get_json(silent=True))
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        return jsonify({'node': node, 'token': token,
                        'install': _install_command(node['name'], token)}), 201

    @app.route('/api/nodes/<int:nid>', methods=['GET', 'PATCH', 'DELETE'])
    def api_node(nid):
        if nodes.get(nid) is None:
            return jsonify({'error': 'not found'}), 404
        if request.method == 'GET':
            return jsonify(nodes.get(nid))
        if request.method == 'DELETE':
            if not nodes.delete(nid):
                return jsonify({'error': 'the local node cannot be deleted'}), 400
            return jsonify({'status': 'deleted'})
        try:
            return jsonify(nodes.update(nid, request.get_json(silent=True)))
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/nodes/<int:nid>/token', methods=['POST'])
    def api_node_token(nid):
        node = nodes.get(nid)
        if node is None:
            return jsonify({'error': 'not found'}), 404
        token = nodes.rotate(nid)
        if token is None:
            return jsonify({'error': 'only relay nodes have tokens'}), 400
        return jsonify({'node': nodes.get(nid), 'token': token,
                        'install': _install_command(node['name'], token)})

    @app.route('/api/nodes/<int:nid>/commands', methods=['GET', 'POST'])
    def api_node_commands(nid):
        if nodes.get(nid) is None:
            return jsonify({'error': 'not found'}), 404
        if request.method == 'GET':
            return jsonify(nodes.commands(nid, request.args.get('limit', 20)))
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(nodes.queue_command(nid, body.get('port'), body.get('command'))), 201
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    # -- realtime ------------------------------------------------------------
    @app.route('/api/stream')
    def api_stream():
        if bus is None:
            return jsonify({'error': 'stream unavailable'}), 503
        return Response(bus.sse(), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache',
                                 'X-Accel-Buffering': 'no',
                                 'Connection': 'keep-alive'})

    # -- maintenance ---------------------------------------------------------
    @app.route('/api/maintenance')
    def api_maintenance():
        st = retention_mod.db_stats(db)
        st['retention_days'] = retainer.days
        return jsonify(st)

    @app.route('/api/maintenance/prune', methods=['POST'])
    def api_prune():
        body = request.get_json(silent=True) or {}
        days = body.get('days', retainer.days)
        return jsonify(retention_mod.prune(db, days, dry_run=bool(body.get('dry_run'))))

    @app.route('/api/maintenance/vacuum', methods=['POST'])
    def api_vacuum():
        return jsonify(retention_mod.vacuum(db))

    # -- FAA registry --------------------------------------------------------
    @app.route('/api/faa/<basic_id>')
    def api_faa(basic_id):
        return jsonify(faa.lookup(basic_id, force=request.args.get('force') == '1'))

    @app.route('/api/drones/<int:did>/faa', methods=['POST'])
    def api_drone_faa(did):
        r = db.one("SELECT basic_id FROM drones WHERE id=?", (did,))
        if r is None or not r['basic_id']:
            return jsonify({'error': 'drone has no RemoteID serial to look up'}), 400
        return jsonify(faa.lookup(r['basic_id'], force=True))

    # -- settings ------------------------------------------------------------
    @app.route('/api/settings')
    def api_settings():
        return jsonify(settings.all())

    @app.route('/api/settings', methods=['PATCH'])
    def api_settings_update():
        was_auto = settings.get('faa.auto')
        try:
            out = settings.update(request.get_json(silent=True))
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        if out['faa.auto'] and not was_auto:
            faa.sweep()                 # catch up on drones seen while it was off
        return jsonify(out)

    # -- notifications -------------------------------------------------------
    @app.route('/api/notify/channels')
    def api_notify_channels():
        return jsonify(notifier.list())

    @app.route('/api/notify/channels', methods=['POST'])
    def api_notify_create():
        try:
            return jsonify(notifier.create(request.get_json(silent=True))), 201
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/notify/channels/<int:cid>', methods=['GET', 'PATCH', 'DELETE'])
    def api_notify_channel(cid):
        if notifier.get(cid) is None:
            return jsonify({'error': 'not found'}), 404
        if request.method == 'GET':
            return jsonify(notifier.get(cid))
        if request.method == 'DELETE':
            notifier.delete(cid)
            return jsonify({'status': 'deleted'})
        try:
            return jsonify(notifier.update(cid, request.get_json(silent=True)))
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/notify/channels/<int:cid>/test', methods=['POST'])
    def api_notify_test(cid):
        res = notifier.test(cid)
        return (jsonify(res), 200) if res else (jsonify({'error': 'not found'}), 404)

    @app.route('/api/notify/log')
    def api_notify_log():
        return jsonify(notifier.log(request.args.get('limit', 100)))

    # -- geofences -----------------------------------------------------------
    @app.route('/api/geofences')
    def api_fences():
        return jsonify(fences.list())

    @app.route('/api/geofences', methods=['POST'])
    def api_fence_create():
        try:
            return jsonify(fences.create(request.get_json(silent=True) or {})), 201
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    @app.route('/api/geofences/<int:fid>', methods=['PATCH', 'DELETE'])
    def api_fence_edit(fid):
        if request.method == 'DELETE':
            fences.delete(fid)
            return jsonify({'status': 'deleted'})
        try:
            f = fences.update(fid, request.get_json(silent=True) or {})
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        return (jsonify(f), 200) if f else (jsonify({'error': 'not found'}), 404)

    @app.route('/api/geofence_events')
    def api_fence_events():
        return jsonify(fences.events(request.args.get('limit', 100),
                                     request.args.get('fence_id')))

    # -- offline tiles -------------------------------------------------------
    @app.route('/tiles/<name>/<int:z>/<int:x>/<int:y>.<ext>')
    def serve_tile(name, z, x, y, ext):
        blob, mime, gz = tiles_svc.get_tile(name, z, x, y)
        if blob is None:
            return ('', 404)
        resp = app.make_response(blob)
        resp.headers['Content-Type'] = mime
        resp.headers['Cache-Control'] = 'public, max-age=2592000, immutable'
        if gz:
            resp.headers['Content-Encoding'] = 'gzip'
        return resp

    @app.route('/api/offline_layers')
    def api_offline_layers():
        return jsonify({'layers': tiles_svc.list_layers(),
                        'dir': tiles_svc.TILES_DIR})

    @app.route('/styles/<name>.json')
    def serve_style(name):
        style = tiles_svc.style_json(name, request.url_root)
        return (jsonify(style), 200) if style else (jsonify({'error': 'no style'}), 404)

    # -- serial ports --------------------------------------------------------
    @app.route('/api/ports')
    def api_ports():
        """Available devices, plus which are selected and connected."""
        return jsonify({
            'ports': list_ports(),
            'selected': serial_mgr.selected if serial_mgr else [],
            'status': serial_mgr.status if serial_mgr else {},
            'counts': serial_mgr.counts if serial_mgr else {},
            'health': (serial_mgr.health() if serial_mgr
                       else {'line_age_s': {}, 'detection_age_s': {}}),
            'available': serial_mgr is not None,
        })

    @app.route('/api/ports', methods=['POST'])
    def api_ports_select():
        if serial_mgr is None:
            return jsonify({'error': 'serial ingest unavailable'}), 503
        body = request.get_json(silent=True) or {}
        ports = body.get('ports')
        if not isinstance(ports, list):
            return jsonify({'error': 'expected {"ports": [...]}'}), 400
        return jsonify({'selected': serial_mgr.select(ports)})

    @app.route('/api/ports/send', methods=['POST'])
    def api_ports_send():
        if serial_mgr is None:
            return jsonify({'error': 'serial ingest unavailable'}), 503
        body = request.get_json(silent=True) or {}
        ok = serial_mgr.send(body.get('port'), body.get('command') or 'STATUS')
        return jsonify({'sent': ok})

    @app.route('/api/ports/raw')
    def api_ports_raw():
        """Recent raw serial lines, oldest first, from every receiver.

        ?port= narrows to one source - `home/ttyACM0`, `north/ttyACM0` - (default:
        all), ?limit= caps the count (default 200, max 500 - the per-source
        buffer size), and ?since=<seq>&boot= returns only lines after that
        sequence number, so the page polls with the `seq` of the last line it
        has plus the `boot` it came with. Sequence numbers restart with the
        process: a `boot` that no longer matches means the server restarted, so
        the stale cursor is dropped, the recent window is sent instead, and
        `reset` is set so the page can mark the seam. `serial` says whether
        this machine reads ports itself; remote nodes' lines arrive either way.
        """
        port = request.args.get('port') or None
        try:
            limit = int(request.args.get('limit', 200))
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(limit, 500))
        try:
            since = int(request.args['since'])
            if since < 0:
                since = None
        except (KeyError, TypeError, ValueError):
            since = None
        boot = request.args.get('boot')
        reset = bool(boot) and boot != rawlog.boot
        if serial_mgr is not None:
            out = serial_mgr.raw_lines(port=port, since=None if reset else since, limit=limit)
        else:
            out = rawlog.lines(source=port, since=None if reset else since, limit=limit)
        out.update(log_path=rawlog.path, reset=reset, log_dropped=rawlog.dropped,
                   available=True, serial=serial_mgr is not None, now=time.time())
        return jsonify(out)

    # -- flights -------------------------------------------------------------
    @app.route('/api/flights')
    def api_flights():
        return jsonify(queries.list_flights(db, request.args))

    @app.route('/api/flights/paths')
    def api_flight_paths():
        ids = [int(i) for i in (request.args.get('ids') or '').split(',') if i.strip().isdigit()]
        return jsonify(queries.flight_paths(db, ids[:500]))

    @app.route('/api/flights/<int:fid>')
    def api_flight(fid):
        f = queries.get_flight(db, fid)
        return (jsonify(f), 200) if f else (jsonify({'error': 'not found'}), 404)

    @app.route('/api/flights/<int:fid>/detections')
    def api_flight_detections(fid):
        return jsonify(queries.flight_detections(
            db, fid, request.args.get('limit', 1000), request.args.get('offset', 0)))

    @app.route('/api/flights/<int:fid>', methods=['PATCH'])
    def api_flight_update(fid):
        body = request.get_json(silent=True) or {}
        for col in ('label', 'notes'):
            if col in body:
                db.execute("UPDATE flights SET {0}=? WHERE id=?".format(col),
                           (body[col] or None, fid))
        return jsonify(queries.get_flight(db, fid))

    @app.route('/api/flights/merge', methods=['POST'])
    def api_flights_merge():
        body = request.get_json(silent=True) or {}
        res = edit.merge_flights(db, body.get('ids') or [])
        return jsonify(res), (400 if 'error' in res else 200)

    @app.route('/api/flights/<int:fid>/recompute', methods=['POST'])
    def api_flight_recompute(fid):
        edit.recompute(db, fid)
        return jsonify(queries.get_flight(db, fid))

    @app.route('/api/flights/<int:fid>/reassign', methods=['POST'])
    def api_flight_reassign(fid):
        body = request.get_json(silent=True) or {}
        res = edit.reassign_flight(db, fid, body.get('drone_id'))
        return jsonify(res), (400 if 'error' in res else 200)

    # -- live ----------------------------------------------------------------
    @app.route('/api/live')
    def api_live():
        """Snapshot of everything currently airborne. Small and bounded."""
        out = []
        now = time.time()
        for st in list(sess.open_flights.values()):
            if st.delayed and now - st.last_ts > sess.gap_s:
                continue        # a backlog flight, kept open only while it is delivered
            row = queries.get_flight(db, st.flight_id) or {}
            row.update({
                'lat': st.end_lat, 'lon': st.end_lon,
                'last_ts': st.last_ts,
                'det_count': st.det_count, 'gps_count': st.gps_count,
                'distance_m': st.path_len_m,
                'tail': st.points[-200:],
            })
            out.append(row)
        return jsonify({'t': time.time(), 'gap_s': sess.gap_s, 'flights': out})

    # -- identity ------------------------------------------------------------
    @app.route('/api/drones')
    def api_drones():
        return jsonify(queries.list_drones(db, request.args))

    @app.route('/api/drones/<int:did>', methods=['PATCH'])
    def api_drone_update(did):
        body = request.get_json(silent=True) or {}
        if 'color' in body:
            color = body['color'] or None
            # Written into style attributes and map strokes, so only a plain
            # #rrggbb gets through - checked before anything is written, so a bad
            # value cannot half-apply the edit. null clears it back to the
            # group's colour or the default.
            if color is not None and not re.fullmatch(r'#[0-9a-fA-F]{6}', str(color)):
                return jsonify({'error': 'color must be #rrggbb or null'}), 400
            db.execute("UPDATE drones SET color=? WHERE id=?",
                       (color.lower() if color else None, did))
        for col in ('label', 'tag', 'notes'):
            if col in body:
                db.execute("UPDATE drones SET {0}=? WHERE id=?".format(col),
                           (body[col] or None, did))
        if 'group_id' in body:
            gid = body['group_id']
            db.execute("UPDATE drones SET group_id=? WHERE id=?",
                       (int(gid) if gid else None, did))
        _forget_drone(did)
        row = db.one("SELECT * FROM drones WHERE id=?", (did,))
        return jsonify(dict(row) if row else {}), (200 if row else 404)

    @app.route('/api/drones/<int:did>/summary')
    def api_drone_summary(did):
        d = edit.drone_summary(db, did)
        return (jsonify(d), 200) if d else (jsonify({'error': 'not found'}), 404)

    @app.route('/api/drones/merge', methods=['POST'])
    def api_drones_merge():
        body = request.get_json(silent=True) or {}
        res = edit.merge_drones(db, body.get('into_id'), body.get('from_ids') or [])
        return jsonify(res), (400 if 'error' in res else 200)

    @app.route('/api/groups')
    def api_groups():
        return jsonify(queries.list_groups(db))

    @app.route('/api/groups', methods=['POST'])
    def api_group_create():
        body = request.get_json(silent=True) or {}
        name = (body.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'name required'}), 400
        try:
            cur = db.execute("INSERT INTO groups(name, kind, color, notes) VALUES(?,?,?,?)",
                             (name, body.get('kind'), body.get('color') or '#4fc3f7',
                              body.get('notes')))
        except Exception:
            return jsonify({'error': 'group already exists'}), 409
        return jsonify(dict(db.one("SELECT * FROM groups WHERE id=?", (cur.lastrowid,)))), 201

    @app.route('/api/groups/<int:gid>', methods=['PATCH', 'DELETE'])
    def api_group_edit(gid):
        if request.method == 'DELETE':
            db.execute("DELETE FROM groups WHERE id=?", (gid,))
            return jsonify({'status': 'deleted'})
        body = request.get_json(silent=True) or {}
        for col in ('name', 'kind', 'color', 'notes'):
            if col in body:
                db.execute("UPDATE groups SET {0}=? WHERE id=?".format(col), (body[col], gid))
        return jsonify(dict(db.one("SELECT * FROM groups WHERE id=?", (gid,))))

    # -- analysis ------------------------------------------------------------
    @app.route('/api/stats')
    def api_stats():
        return jsonify(queries.stats(db))

    @app.route('/api/analysis/activity')
    def api_activity():
        return jsonify(queries.activity(db, request.args.get('bucket', 'hour')))

    # The Analysis page. All three read the same filter params as the flight
    # table (from, to, group_id, drone_id, tag), so every view shows one slice.
    @app.route('/api/analysis/overview')
    def api_analysis_overview():
        try:
            return jsonify(analysis.overview(db, request.args))
        except ValueError as e:
            return jsonify({'error': 'bad filter: %s' % e}), 400

    @app.route('/api/analysis/cells')
    def api_analysis_cells():
        try:
            return jsonify(analysis.cells(db, request.args, request.args.get('kind', 'launch'),
                                          request.args.get('cell_deg') or 0.0025))
        except ValueError as e:
            return jsonify({'error': 'bad filter: %s' % e}), 400

    @app.route('/api/analysis/radio')
    def api_analysis_radio():
        try:
            return jsonify(analysis.radio(db, request.args))
        except ValueError as e:
            return jsonify({'error': 'bad filter: %s' % e}), 400

    @app.route('/api/analysis/launch_points')
    def api_launch_points():
        cell = float(request.args.get('cell_deg') or 0.0025)
        return jsonify(queries.launch_points(db, cell, request.args))

    # -- export --------------------------------------------------------------
    @app.route('/api/export/flights.csv')
    def export_csv():
        rows = queries.list_flights(db, dict(request.args, limit='500', offset='0'))
        return Response(flights_csv(db, request.args),
                        mimetype='text/csv',
                        headers={'Content-Disposition':
                                 'attachment; filename=flights.csv'})

    @app.route('/api/export/flights.kml')
    def export_kml():
        return Response(flights_kml(db, request.args),
                        mimetype='application/vnd.google-earth.kml+xml',
                        headers={'Content-Disposition':
                                 'attachment; filename=flights.kml'})

    @app.route('/api/export/flights.gpx')
    def export_gpx():
        return Response(flights_gpx(db, request.args),
                        mimetype='application/gpx+xml',
                        headers={'Content-Disposition':
                                 'attachment; filename=flights.gpx'})

    return app


def main(argv=None):
    ap = argparse.ArgumentParser(description='flightlog - flight-first RemoteID logger')
    ap.add_argument('--web-port', type=int, default=5001)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--db', default=None)
    ap.add_argument('--gap', type=float, default=DEFAULT_GAP_S)
    ap.add_argument('--resume-window', type=float, default=DEFAULT_RESUME_S,
                    help='continue a flight whose drone reappears within this many '
                         'seconds from the same operator position (0 = plain gap rule)')
    ap.add_argument('--debug', action='store_true')
    ap.add_argument('--retention-days', type=int, default=0,
                    help='drop raw detection rows for closed flights older than N days '
                         '(0 = keep everything; summaries and paths are always kept)')
    ap.add_argument('--no-serial', action='store_true',
                    help='HTTP ingest only; leaves the ports free for mesh-mapper.py')
    ap.add_argument('--no-serial-log', action='store_true',
                    help='do not write raw serial traffic to flightlog_serial.log '
                         '(the Sources page still shows recent lines from memory)')
    args = ap.parse_args(argv)

    # Leaflet and the fonts come from the repo's static/ dir via the /vendor
    # route. If only flightlog/ was copied somewhere, every page still loads and
    # the map is simply blank - which gives no clue in the browser.
    vendor = os.path.join(BASE_DIR, 'static', 'leaflet', 'leaflet.js')
    if not os.path.exists(vendor):
        print("WARNING: {0} is missing, so the map will be blank.".format(vendor))
        print("         Copy the repo's static/ directory next to flightlog/.")

    app = create_app(args.db, gap_s=args.gap, serial_enabled=not args.no_serial,
                     retention_days=args.retention_days, resume_s=args.resume_window,
                     serial_log=not args.no_serial_log)
    print("flightlog on http://{0}:{1}  (db: {2})".format(
        args.host, args.web_port, app.config['DB'].path))
    app.run(host=args.host, port=args.web_port, debug=args.debug, threaded=True)


if __name__ == '__main__':
    main()
