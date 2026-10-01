/* Live view, driven by pushed deltas.
 *
 * One /api/live snapshot on load, then everything arrives over SSE. There is no
 * poll loop and no periodic full-state fetch - the legacy UI's 100ms
 * /api/detections poll plus a 15s re-download of every path is exactly what
 * this replaces. EventSource reconnects on its own; the snapshot is re-fetched
 * on reconnect so a dropped connection cannot leave the map stale.
 */
(function () {
  'use strict';

  var mv = new MapView('map');
  var hint = document.getElementById('mapHint');
  // Receivers, coloured by status; refreshed now and then, not streamed.
  mv.showNodes();
  setInterval(function () { if (!document.hidden) mv.showNodes(); }, 30000);
  var flights = {};                 // flight_id -> {meta, points[], marker, line}
  var fitted = false;

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;');
  }
  function nameOf(f) {
    return f.label || f.drone_label || f.basic_id || f.mac || ('flight ' + f.flight_id);
  }

  function redraw() {
    var paths = {}, meta = {};
    Object.keys(flights).forEach(function (id) {
      var f = flights[id];
      if (!f.points.length) return;
      paths[id] = { points: f.points, color: f.color };
      var last = f.points[f.points.length - 1];
      meta[id] = { popup: '<b>' + esc(nameOf(f)) + '</b><br>'
        + (f.alt !== null && f.alt !== undefined ? f.alt + ' m MSL<br>' : '')
        + (f.speed_ms ? f.speed_ms.toFixed(1) + ' m/s<br>' : '')
        + last[0].toFixed(5) + ', ' + last[1].toFixed(5) };
    });
    mv.setPaths(paths, meta);
    var n = Object.keys(paths).length;
    hint.style.display = n ? 'none' : 'block';
    if (!fitted && n) { mv.fit(); fitted = true; }
    document.getElementById('headerStats').innerHTML =
      '<span><b>' + Object.keys(flights).length + '</b> airborne</span>'
      + '<span class="dim" id="conn">live</span>';
  }

  function snapshot() {
    return fetch('/api/live').then(function (r) { return r.json(); }).then(function (d) {
      flights = {};
      (d.flights || []).forEach(function (f) {
        flights[f.id] = {
          flight_id: f.id, label: f.drone_label, basic_id: f.basic_id, mac: f.mac,
          color: f.display_color, alt: f.max_alt_m, speed_ms: null,
          points: (f.tail || []).slice()
        };
      });
      redraw();
    });
  }

  function onPositions(d) {
    var touched = false;
    (d.flights || []).forEach(function (p) {
      var f = flights[p.flight_id];
      if (!f) {
        // A flight we have not seen - pull a fresh snapshot rather than
        // guessing at its history.
        snapshot();
        return;
      }
      if (p.lat !== null && p.lat !== undefined) {
        f.points.push([p.lat, p.lon]);
        if (f.points.length > 400) f.points.shift();
        touched = true;
      }
      f.alt = p.alt;
      f.speed_ms = p.speed_ms;
      if (p.label) f.label = p.label;
    });
    if (touched) redraw();
  }

  snapshot().then(function () {
    var es = new EventSource('/api/stream');
    es.addEventListener('positions', function (e) { onPositions(JSON.parse(e.data)); });
    es.addEventListener('flight:open', function () { snapshot(); });
    es.addEventListener('flight:close', function (e) {
      var d = JSON.parse(e.data);
      delete flights[d.flight_id];
      redraw();
    });
    es.addEventListener('geofence', function (e) {
      var d = JSON.parse(e.data);
      hint.style.display = 'block';
      hint.textContent = d.transition.toUpperCase() + ' ' + d.fence
        + ' - ' + (d.label || ('drone ' + d.drone_id));
      setTimeout(function () { hint.textContent = 'Nothing airborne right now'; }, 6000);
    });
    es.onerror = function () {
      var c = document.getElementById('conn');
      if (c) { c.textContent = 'reconnecting'; c.style.color = 'var(--warn)'; }
    };
    es.onopen = function () {
      var c = document.getElementById('conn');
      if (c) { c.textContent = 'live'; c.style.color = ''; }
      snapshot();
    };
  });
})();
