/* Analysis: one filter row scoping every view below it.
 *
 * Everything is aggregated server-side (analysis.py) from the same filtered
 * slice of flights, so the tiles, the heatmap, the tables and the map always
 * agree. Colours are dark steps of the documented dataviz palette: one blue
 * for bars, one blue ramp for magnitude, orange for map markers. A drone's
 * own colour only ever appears as the swatch beside its name - there are far
 * more drones than any chart could tell apart by hue.
 */
(function () {
  'use strict';
  var el = function (id) { return document.getElementById(id); };
  var DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var ROWS = [1, 2, 3, 4, 5, 6, 0];              // Mon..Sun, so the weekend sits together
  var TAGS = ['unknown', 'civilian', 'police', 'government', 'military', 'commercial', 'known'];
  // Sequential blue, anchored for the dark surface: few (dark) -> many (light).
  var RAMP = ['#104281', '#1c5cab', '#2a78d6', '#5598e7', '#86b6ef', '#b7d3f6'];
  var ZERO = '#18202b';
  var SPOT = '#d95926';                          // palette orange, dark step
  var TOP_ROWS = 100;
  var state = { kind: 'launch', sort: 'flights', dir: -1, all: false, data: null };
  var gen = 0;

  // -- small helpers --------------------------------------------------------------
  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/&/g, '&amp;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  // Group colours are not validated on the way in; only a plain hex reaches a style.
  function safeColor(c) { return /^#[0-9a-f]{6}$/i.test(c || '') ? c : '#6b7c90'; }
  function json(r) {
    return r.json().then(function (j) {
      if (!r.ok && j && !j.error) j.error = 'HTTP ' + r.status;
      return j;
    });
  }
  function fmtInt(n) { return Number(n || 0).toLocaleString(); }
  function fmtDur(s) {
    s = Math.round(s || 0);
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60);
    if (m < 60) return m + 'm';
    var h = Math.floor(m / 60);
    return h < 48 ? h + 'h ' + (m % 60) + 'm' : Math.round(h / 24) + 'd ' + (h % 24) + 'h';
  }
  function fmtKm(m) { return (m || 0) >= 10000 ? Math.round(m / 1000) + ' km' : ((m || 0) / 1000).toFixed(1) + ' km'; }
  // "Aug 6" this year, "Aug 6, 2025" otherwise.
  function fmtDate(ts) {
    if (!ts) return '-';
    var d = new Date(ts * 1000), o = { month: 'short', day: 'numeric' };
    if (d.getFullYear() !== new Date().getFullYear()) o.year = 'numeric';
    return d.toLocaleDateString([], o);
  }
  function ago(ts) {
    if (!ts) return '';
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' d ago';
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function hourSpan(h) { return pad(h) + ':00–' + pad((h + 1) % 24) + ':00'; }
  function plural(n, one, many) { return fmtInt(n) + ' ' + (n === 1 ? one : (many || one + 's')); }
  // sqrt scale into the 6 ramp steps; zero stays the empty-cell tone.
  function rampIdx(n, max) { return Math.min(RAMP.length - 1, Math.floor(Math.sqrt(n / max) * RAMP.length)); }
  function heat(n, max) { return n ? RAMP[rampIdx(n, max)] : ZERO; }
  function inkOn(n, max) { return n && rampIdx(n, max) >= 4 ? '#0b0b0b' : '#ffffff'; }
  // Tooltip content lives in data-v (the value, shown strong) and data-l.
  function tipAttrs(v, l) { return ' data-v="' + esc(v) + '" data-l="' + esc(l) + '"'; }

  // -- filters ---------------------------------------------------------------------
  function dayStart(ymd) { var a = ymd.split('-'); return new Date(+a[0], a[1] - 1, +a[2]).getTime() / 1000; }
  function ymd(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }

  function params() {
    var p = new URLSearchParams(), r = el('fRange').value;
    if (r === 'custom') {
      if (el('fFrom').value) p.set('from', Math.floor(dayStart(el('fFrom').value)));
      if (el('fTo').value) p.set('to', Math.floor(dayStart(el('fTo').value)) + 86399);
    } else if (r !== 'all') {
      p.set('from', Math.floor(Date.now() / 1000 - parseFloat(r) * 86400));
    }
    if (el('fGroup').value) p.set('group_id', el('fGroup').value);
    if (el('fDrone').value) p.set('drone_id', el('fDrone').value);
    if (el('fTag').value) p.set('tag', el('fTag').value);
    if (el('fNode').value) p.set('rx_node', el('fNode').value);
    return p;
  }

  // The view lives in the URL, so it can be bookmarked or shared.
  function saveUrl() {
    var u = new URLSearchParams(), r = el('fRange').value;
    if (r !== 'all') u.set('range', r);
    if (r === 'custom') {
      if (el('fFrom').value) u.set('from', el('fFrom').value);
      if (el('fTo').value) u.set('to', el('fTo').value);
    }
    if (el('fGroup').value) u.set('group', el('fGroup').value);
    if (el('fDrone').value) u.set('drone', el('fDrone').value);
    if (el('fTag').value) u.set('tag', el('fTag').value);
    if (el('fNode').value) u.set('node', el('fNode').value);
    if (state.kind === 'pilot') u.set('spots', 'pilot');
    if (state.cover) u.set('cover', '1');
    var q = u.toString();
    history.replaceState(null, '', location.pathname + (q ? '?' + q : ''));
  }

  function ensureOption(sel, value, label) {
    if (!value) return;
    for (var i = 0; i < sel.options.length; i++) if (sel.options[i].value === String(value)) return;
    sel.add(new Option(label, value));
  }

  function loadUrl() {
    var u = new URLSearchParams(location.search);
    var r = u.get('range');
    if (r && el('fRange').querySelector('option[value="' + r.replace(/"/g, '') + '"]')) el('fRange').value = r;
    if (u.get('from')) el('fFrom').value = u.get('from');
    if (u.get('to')) el('fTo').value = u.get('to');
    ensureOption(el('fGroup'), u.get('group'), 'group #' + u.get('group'));
    ensureOption(el('fDrone'), u.get('drone'), 'drone #' + u.get('drone'));
    ensureOption(el('fNode'), u.get('node'), 'receiver #' + u.get('node'));
    el('fGroup').value = u.get('group') || '';
    el('fDrone').value = u.get('drone') || '';
    el('fNode').value = u.get('node') || '';
    el('fTag').value = TAGS.indexOf(u.get('tag')) !== -1 ? u.get('tag') : '';
    state.kind = u.get('spots') === 'pilot' ? 'pilot' : 'launch';
    state.cover = u.get('cover') === '1';
    showCustom();
    showKind();
  }

  function showCustom() {
    var custom = el('fRange').value === 'custom';
    el('fCustom').hidden = !custom;
    if (custom && !el('fFrom').value && !el('fTo').value) {
      var now = new Date();
      el('fTo').value = ymd(now);
      el('fFrom').value = ymd(new Date(now.getTime() - 29 * 86400000));
    }
  }

  function loadOptions() {
    el('fTag').innerHTML = '<option value="">all tags</option>'
      + TAGS.map(function (t) { return '<option>' + t + '</option>'; }).join('');
    return Promise.all([fetch('/api/groups').then(json), fetch('/api/drones').then(json),
                        fetch('/api/nodes').then(json)])
      .then(function (res) {
        el('fNode').innerHTML = '<option value="">any receiver</option>' + (res[2] || []).map(function (n) {
          return '<option value="' + n.id + '">' + esc(n.name) + '</option>';
        }).join('');
        el('fGroup').innerHTML = '<option value="">all groups</option>' + (res[0] || []).map(function (g) {
          return '<option value="' + g.id + '">' + esc(g.name) + '</option>';
        }).join('');
        var ds = (res[1] || []).slice().sort(function (a, b) {
          return (a.label || a.basic_id || '').localeCompare(b.label || b.basic_id || '');
        });
        el('fDrone').innerHTML = '<option value="">all drones</option>' + ds.map(function (d) {
          var model = [d.faa_make, d.faa_model].filter(Boolean).join(' ');
          return '<option value="' + d.id + '">' + esc((d.label || d.basic_id || 'drone #' + d.id)
            + (model ? ' - ' + model : '')) + '</option>';
        }).join('');
      })
      .catch(function () { /* the page still works unfiltered */ });
  }

  // -- tooltip -------------------------------------------------------------------------
  var tip = el('tip');
  function showTip(t, x, y) {
    tip.textContent = '';
    var b = document.createElement('b');
    b.textContent = t.getAttribute('data-v');
    var s = document.createElement('span');
    s.textContent = t.getAttribute('data-l');
    tip.appendChild(b);
    tip.appendChild(s);
    tip.hidden = false;
    var w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(x + 14, window.innerWidth - w - 8) + 'px';
    tip.style.top = (y + 16 + h > window.innerHeight ? y - h - 10 : y + 16) + 'px';
  }
  document.addEventListener('pointermove', function (e) {
    var t = e.target.closest ? e.target.closest('[data-v]') : null;
    if (t) showTip(t, e.clientX, e.clientY); else tip.hidden = true;
  });
  document.addEventListener('focusin', function (e) {
    var t = e.target.closest ? e.target.closest('[data-v]') : null;
    if (!t) { tip.hidden = true; return; }
    var r = t.getBoundingClientRect();
    showTip(t, r.left, r.bottom);
  });
  document.addEventListener('scroll', function () { tip.hidden = true; }, true);

  // -- tiles -----------------------------------------------------------------------------
  function renderTiles(t) {
    var ranged = el('fRange').value !== 'all';
    var items = [
      ['Flights', fmtInt(t.flights), fmtKm(t.distance_m) + ' flown'],
      ['Drones', fmtInt(t.drones), ''],
      ['New drones', fmtInt(t.new_drones), ranged ? 'first ever seen in this range' : 'all time: every drone'],
      ['Airtime', fmtDur(t.airtime_s), '']
    ];
    el('tiles').innerHTML = items.map(function (it) {
      return '<div class="card an-tile"><div class="lbl">' + it[0] + '</div>'
        + '<div class="val">' + it[1] + '</div><div class="sub">' + esc(it[2]) + '</div></div>';
    }).join('');
  }

  // -- heatmap -------------------------------------------------------------------------------
  function renderHeat(h) {
    var cells = {}, max = 0, byDow = {}, byHour = {}, maxD = 0, maxH = 0, peakH = -1;
    h.cells.forEach(function (c) { cells[c.dow + ':' + c.hour] = c; max = Math.max(max, c.flights); });
    h.by_dow.forEach(function (c) { byDow[c.dow] = c; maxD = Math.max(maxD, c.flights); });
    h.by_hour.forEach(function (c) {
      byHour[c.hour] = c;
      if (c.flights > maxH) { maxH = c.flights; peakH = c.hour; }
    });
    var nums = el('hmNums').checked, html = '';
    ROWS.forEach(function (dow) {
      html += '<div class="rl">' + DOW[dow] + '</div>';
      for (var hr = 0; hr < 24; hr++) {
        var c = cells[dow + ':' + hr], n = c ? c.flights : 0;
        html += '<div class="c" style="background:' + heat(n, max) + ';color:' + inkOn(n, max) + '"'
          + tipAttrs(plural(n, 'flight'), DOW[dow] + ' ' + hourSpan(hr)
            + (c ? ' · ' + plural(c.drones, 'drone') : '')) + '>' + (nums && n ? n : '') + '</div>';
      }
      var d = byDow[dow] || { flights: 0, drones: 0 };
      html += '<div class="rt"' + tipAttrs(plural(d.flights, 'flight'), 'every ' + DOW[dow]
          + ' · ' + plural(d.drones, 'drone')) + '>'
        + '<div class="b" style="width:' + (maxD ? Math.max(1, Math.round(84 * d.flights / maxD)) : 0) + 'px"></div>'
        + d.flights + '</div>';
    });
    html += '<div></div>';
    for (var hr = 0; hr < 24; hr++) {
      var t = byHour[hr] || { flights: 0, drones: 0 };
      html += '<div class="ct"' + tipAttrs(plural(t.flights, 'flight'), hourSpan(hr) + ', any day · '
          + plural(t.drones, 'drone')) + '>'
        + (t.flights && (nums || hr === peakH) ? '<span class="v">' + t.flights + '</span>' : '')
        + '<div class="b" style="height:' + (maxH ? Math.round(40 * t.flights / maxH) : 0) + 'px"></div></div>';
    }
    html += '<div></div><div></div>';
    for (hr = 0; hr < 24; hr++) html += '<div class="hl">' + (hr % 3 === 0 ? pad(hr) : '') + '</div>';
    html += '<div></div>';
    el('heat').innerHTML = html;
    el('hmLegend').innerHTML = max ? '<span>fewer</span>' + RAMP.map(function (c) {
      return '<i style="background:' + c + '"></i>';
    }).join('') + '<span>more (max ' + max + ' in an hour)</span>' : '';
  }

  // -- map ---------------------------------------------------------------------------------------
  var mv = new MapView('map');
  mv._autoCenter = false;
  var cellLayer = L.layerGroup().addTo(mv.map);
  var lastCell = null, cellGen = 0;

  // One cell ~ 40 px on screen at this zoom and latitude, so neighbouring
  // circles (up to 36 px across) sit side by side instead of piling up. Snapped
  // to doubling steps from 0.0003 deg (~35 m) so small zoom changes don't refetch.
  function cellDegFor(z) {
    var lat = mv.map.getCenter().lat * Math.PI / 180;
    var mpp = 156543.03 * Math.cos(lat) / Math.pow(2, z);        // metres per pixel
    var k = Math.round(Math.log((40 * mpp / 111320) / 0.0003) / Math.LN2);
    return 0.0003 * Math.pow(2, Math.min(7, Math.max(0, k)));    // 0.0003 .. 0.0384 deg
  }
  function radius(n, max) { return 5 + 13 * Math.sqrt(n / max); }

  function popup(c, kind) {
    var root = document.createElement('div');
    root.className = 'an-pop';
    var b = document.createElement('b');
    b.textContent = kind === 'pilot' ? plural(c.n, 'flight') + ' flown from here'
                                     : plural(c.n, 'launch', 'launches');
    var sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = plural(c.drones, 'drone') + (c.last ? ' · last ' + ago(c.last) : '');
    root.appendChild(b);
    root.appendChild(sub);
    c.top.forEach(function (d) {
      var r = document.createElement('div');
      r.className = 'r';
      var sw = document.createElement('span');
      sw.className = 'swatch';
      sw.style.background = safeColor(d.color);
      var a = document.createElement('a');
      a.href = '/drone/' + d.id;
      a.textContent = d.name;
      var n = document.createElement('span');
      n.className = 'n';
      n.textContent = d.n;
      r.appendChild(sw); r.appendChild(a); r.appendChild(n);
      root.appendChild(r);
    });
    if (c.drones > c.top.length) {
      var more = document.createElement('div');
      more.className = 'sub';
      more.textContent = '+ ' + plural(c.drones - c.top.length, 'more drone');
      root.appendChild(more);
    }
    return root;
  }

  function mapLegend(max, d) {
    var box = el('mapLegend');
    if (!max) { box.hidden = true; return; }
    var vals = [1];
    if (max >= 8) vals.push(Math.round(max / 4));
    if (max > 1) vals.push(max);
    var x = 4, svg = '';
    vals.forEach(function (v) {
      var r = radius(v, max);
      svg += '<circle cx="' + (x + r) + '" cy="20" r="' + r + '" fill="' + SPOT + '" stroke="#fff" stroke-width="2"/>'
        + '<text x="' + (x + 2 * r + 4) + '" y="24" fill="#6b7c90" font-size="11">' + v + '</text>';
      x += 2 * r + 10 + String(v).length * 7;
    });
    var m = d.cell_deg * 111000;
    box.innerHTML = '<svg width="' + x + '" height="40">' + svg + '</svg>'
      + (d.kind === 'pilot' ? 'flights per operator spot' : 'launches per spot')
      + ' · grid ≈ ' + (m >= 1000 ? (m / 1000).toFixed(1) + ' km' : Math.round(m) + ' m');
    box.hidden = false;
  }

  function drawCells(d) {
    cellLayer.clearLayers();
    var cs = d.cells, max = cs.length ? cs[0].n : 0, labelled = 0;
    cs.forEach(function (c) {
      var r = radius(c.n, max);
      var m = L.circleMarker([c.lat, c.lon], {
        radius: r, color: '#ffffff', weight: 2, opacity: 1, fillColor: SPOT, fillOpacity: 0.92
      });
      m.bindPopup(popup(c, d.kind));
      // A count label only where it fits inside the circle; ink on the orange
      // clears 5:1, and it is the relief channel over water where the fill is weak.
      if (labelled < 250 && String(c.n).length * 6.5 <= 2 * r - 6) {
        m.bindTooltip(String(c.n), { permanent: true, direction: 'center', className: 'an-count', opacity: 1 });
        labelled++;
      }
      m.addTo(cellLayer);
    });
    mapLegend(max, d);
    el('mapNote').textContent = cs.length
      ? 'circle area = ' + (d.kind === 'pilot' ? 'flights flown from that spot' : 'launches from that spot')
        + ' · click one for the drones'
        + (d.kind === 'pilot' ? ' · DJI DroneID drones report their takeoff point' : '')
      : (d.kind === 'pilot' ? 'no operator positions in this range' : 'no launch points in this range');
    return cs;
  }

  function loadCells(fit) {
    var my = ++cellGen, cd = cellDegFor(mv.map.getZoom());
    lastCell = cd;
    var p = params();
    p.set('kind', state.kind);
    p.set('cell_deg', cd);
    fetch('/api/analysis/cells?' + p.toString()).then(json).then(function (d) {
      if (my !== cellGen || d.error) return;
      var cs = drawCells(d);
      if (fit && cs.length) {
        // A zoom change here reloads the cells at that zoom's finer grid.
        mv.map.fitBounds(L.latLngBounds(cs.map(function (c) { return [c.lat, c.lon]; })),
                         { padding: [48, 48], maxZoom: 16 });
      }
    });
  }
  mv.map.on('zoomend', function () {
    if (cellDegFor(mv.map.getZoom()) !== lastCell) loadCells(false);
  });

  function showKind() {
    el('kLaunch').setAttribute('aria-pressed', String(state.kind === 'launch'));
    el('kPilot').setAttribute('aria-pressed', String(state.kind === 'pilot'));
    el('kCover').setAttribute('aria-pressed', String(!!state.cover));
  }

  // -- receivers --------------------------------------------------------------------------
  // Receivers sit on the map as diamonds; "Coverage" adds a circle per receiver
  // at its far (95th percentile) range - where another node would extend reach.
  var coverLayer = L.layerGroup().addTo(mv.map);

  function fmtM(m) {
    if (m === null || m === undefined) return '-';
    return m >= 1000 ? (m / 1000).toFixed(m >= 10000 ? 0 : 1) + ' km' : Math.round(m) + ' m';
  }

  function drawReceivers() {
    var r = state.rx;
    coverLayer.clearLayers();
    if (!r || !r.nodes) return;
    mv.setNodes(r.nodes.map(function (n) {
      return { id: n.id, name: n.name, kind: n.kind, lat: n.lat, lon: n.lon,
               status: n.status || 'online', last_detection_at: n.last_heard };
    }));
    if (!state.cover) return;
    r.nodes.forEach(function (n) {
      if (n.lat === null || !n.range) return;
      L.circle([n.lat, n.lon], {
        radius: n.range.p95_m, color: '#4fc3f7', weight: 1.5, dashArray: '6 5',
        fillColor: '#4fc3f7', fillOpacity: 0.06, interactive: false
      }).addTo(coverLayer);
    });
  }

  function renderReceivers(r) {
    state.rx = r && !r.error ? r : null;
    if (!state.rx) { el('rx').innerHTML = '<tr><td class="an-empty">Could not load.</td></tr>'; return; }
    var rows = r.nodes.filter(function (n) { return n.flights || n.kind === 'local' || n.lat !== null; });
    if (!rows.length) { el('rx').innerHTML = '<tr><td class="an-empty">No receivers yet.</td></tr>'; return; }
    var maxF = Math.max.apply(null, rows.map(function (n) { return n.flights; }).concat([1]));
    el('rx').innerHTML = '<thead><tr><th class="nosort">Receiver</th><th class="nosort r">Flights</th>'
      + '<th class="nosort r">Drones</th><th class="nosort r">Receptions</th><th class="nosort r">Best RSSI</th>'
      + '<th class="nosort r" title="median distance to what it heard">Typical range</th>'
      + '<th class="nosort r" title="95th percentile">Far range</th><th class="nosort r">Farthest</th>'
      + '</tr></thead><tbody>' + rows.map(function (n) {
        var st = n.status || '';
        var range = n.lat === null
          ? '<td colspan="3" class="dim" style="text-align:right">no location set (Nodes page)</td>'
          : !n.range ? '<td colspan="3" class="dim" style="text-align:right">nothing heard here</td>'
          : '<td class="num"' + tipAttrs(fmtM(n.range.p50_m), 'half of what ' + n.name + ' heard was closer')
            + '>' + fmtM(n.range.p50_m) + '</td>'
            + '<td class="num"' + tipAttrs(fmtM(n.range.p95_m), '95% was closer; the coverage circle')
            + '>' + fmtM(n.range.p95_m) + '</td>'
            + '<td class="num"' + tipAttrs(fmtM(n.range.max_m), 'over ' + plural(n.range.samples, 'position'))
            + '>' + fmtM(n.range.max_m) + '</td>';
        return '<tr class="go" data-node="' + n.id + '">'
          + '<td>' + esc(n.name) + ' <span class="badge st-' + esc(st) + '">'
          + esc((MapView.NODE_STATUS || {})[st] || st || '?') + '</span></td>'
          + '<td class="num">' + fmtInt(n.flights) + '<span class="ibar" style="width:'
          + Math.max(2, Math.round(48 * n.flights / maxF)) + 'px"></span></td>'
          + '<td class="num">' + fmtInt(n.drones) + '</td><td class="num">' + fmtInt(n.receptions) + '</td>'
          + '<td class="num">' + (n.max_rssi !== null ? n.max_rssi + ' dBm' : '-') + '</td>'
          + range + '</tr>';
      }).join('') + '</tbody>';
  }

  el('rx').addEventListener('click', function (e) {
    var tr = e.target.closest('tr[data-node]');
    if (!tr) return;
    el('fNode').value = tr.getAttribute('data-node');
    refresh();
  });
  el('kCover').addEventListener('click', function () {
    state.cover = !state.cover;
    showKind();
    saveUrl();
    drawReceivers();
  });
  el('kLaunch').addEventListener('click', function () {
    if (state.kind === 'launch') return;
    state.kind = 'launch'; showKind(); saveUrl(); loadCells(true);
  });
  el('kPilot').addEventListener('click', function () {
    if (state.kind === 'pilot') return;
    state.kind = 'pilot'; showKind(); saveUrl(); loadCells(true);
  });

  // -- groups & drones -----------------------------------------------------------------
  function renderGroups(gs) {
    if (!gs.length) { el('groups').innerHTML = '<tr><td class="an-empty">No flights match these filters.</td></tr>'; return; }
    el('groups').innerHTML = '<thead><tr><th class="nosort">Group</th><th class="nosort r">Drones</th>'
      + '<th class="nosort r">Flights</th><th class="nosort r">Airtime</th><th class="nosort">Last seen</th></tr></thead><tbody>'
      + gs.map(function (g) {
        return '<tr' + (g.id ? ' class="go" data-group="' + g.id + '"' : '') + '>'
          + '<td>' + (g.id ? '<span class="swatch" style="background:' + safeColor(g.color) + '"></span>'
            + esc(g.name) : '<span class="dim">no group</span>') + '</td>'
          + '<td class="num">' + fmtInt(g.drones) + '</td><td class="num">' + fmtInt(g.flights) + '</td>'
          + '<td class="num">' + fmtDur(g.airtime_s) + '</td><td>' + fmtDate(g.last_seen) + '</td></tr>';
      }).join('') + '</tbody>';
  }

  var COLS = [
    ['name', 'Drone'], ['model', 'Model'], ['group_name', 'Group'], ['flights', 'Flights', 1],
    ['days', 'Days seen', 1], ['airtime_s', 'Airtime', 1], [null, 'When it flies'],
    [null, 'Home spot'], ['first_seen', 'First'], ['last_seen', 'Last']
  ];

  function renderDrones() {
    var d = state.data;
    if (!d) return;
    var rows = d.drones.slice(), key = state.sort, dir = state.dir;
    rows.sort(function (a, b) {
      var x = a[key], y = b[key];
      if (typeof x === 'string' || typeof y === 'string') {
        x = (x || '￿').toLowerCase(); y = (y || '￿').toLowerCase();
      } else { x = x || 0; y = y || 0; }
      return x < y ? -dir : x > y ? dir : a.id - b.id;
    });
    var shown = state.all ? rows : rows.slice(0, TOP_ROWS);
    var maxF = Math.max.apply(null, rows.map(function (r) { return r.flights; }).concat([1]));
    var ranged = el('fRange').value !== 'all';
    var head = COLS.map(function (c) {
      var arrow = c[0] === key ? '<span class="arrow">' + (dir < 0 ? '▼' : '▲') + '</span>' : '';
      return '<th' + (c[0] ? ' data-sort="' + c[0] + '"' : ' class="nosort"')
        + (c[2] ? ' style="text-align:right"' : '') + '>' + c[1] + arrow + '</th>';
    }).join('');
    var body = shown.map(function (r) {
      var hmax = Math.max.apply(null, r.hours.concat([1]));
      var strip = r.hours.map(function (n, h) {
        return '<i style="background:' + heat(n, hmax) + '"' + tipAttrs(plural(n, 'flight'), hourSpan(h)) + '></i>';
      }).join('');
      var home = r.home
        ? '<button class="linkish home" data-lat="' + r.home.lat + '" data-lon="' + r.home.lon + '"'
          + tipAttrs(r.home.n + ' of ' + r.home.of + ' launches here',
                     r.home.lat.toFixed(5) + ', ' + r.home.lon.toFixed(5) + ' · show on map')
          + '>' + r.home.n + ' of ' + r.home.of + '</button>'
        : '<span class="dim">no fix</span>';
      return '<tr class="go" data-drone="' + r.id + '">'
        + '<td><span class="swatch" style="background:' + safeColor(r.color) + '"></span>'
        + '<a href="/drone/' + r.id + '">' + esc(r.name) + '</a>'
        + (r.id_type === 'DJI' ? ' <span class="badge" title="heard through DJI DroneID">DJI</span>' : '')
        + (ranged && r['new'] ? ' <span class="badge">new</span>' : '') + '</td>'
        + '<td>' + (r.model ? esc(r.model) : '<span class="dim">-</span>') + '</td>'
        + '<td>' + (r.group_name ? esc(r.group_name) : '<span class="dim">-</span>') + '</td>'
        + '<td class="num">' + fmtInt(r.flights) + '<span class="ibar" style="width:'
        + Math.max(2, Math.round(48 * r.flights / maxF)) + 'px"></span></td>'
        + '<td class="num">' + fmtInt(r.days) + '</td>'
        + '<td class="num">' + fmtDur(r.airtime_s) + '</td>'
        + '<td><span class="strip">' + strip + '</span></td>'
        + '<td>' + home + '</td>'
        + '<td class="dim">' + fmtDate(r.first_seen) + '</td><td>' + fmtDate(r.last_seen) + '</td></tr>';
    }).join('');
    el('drones').innerHTML = rows.length
      ? '<thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody>'
      : '<tr><td class="an-empty">No drones match these filters.</td></tr>';
    var more = d.drones_total - shown.length;
    el('dronesMore').innerHTML = (!state.all && rows.length > TOP_ROWS)
      ? '<div style="margin-top:8px"><button id="showAll">Show all ' + fmtInt(rows.length) + '</button></div>'
      : (more > 0 ? '<div class="dim" style="margin-top:8px">+ ' + fmtInt(more)
         + ' less active drones not listed - narrow the filters to see them</div>' : '');
  }

  el('drones').addEventListener('click', function (e) {
    var th = e.target.closest('th[data-sort]');
    if (th) {
      var k = th.getAttribute('data-sort');
      state.dir = state.sort === k ? -state.dir : (k === 'name' || k === 'model' || k === 'group_name' ? 1 : -1);
      state.sort = k;
      renderDrones();
      return;
    }
    var home = e.target.closest('button.home');
    if (home) {
      mv.map.setView([+home.getAttribute('data-lat'), +home.getAttribute('data-lon')], 16);
      el('map').scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    if (e.target.closest('a')) return;                 // the name links to the drone's page
    var tr = e.target.closest('tr[data-drone]');
    if (tr) {
      var id = tr.getAttribute('data-drone');
      var row = state.data.drones.filter(function (r) { return String(r.id) === id; })[0];
      ensureOption(el('fDrone'), id, row ? row.name : 'drone #' + id);
      el('fDrone').value = id;
      refresh();
    }
  });
  el('dronesMore').addEventListener('click', function (e) {
    if (e.target.id === 'showAll') { state.all = true; renderDrones(); }
  });
  el('groups').addEventListener('click', function (e) {
    var tr = e.target.closest('tr[data-group]');
    if (!tr) return;
    el('fGroup').value = tr.getAttribute('data-group');
    refresh();
  });

  // -- models & radio ---------------------------------------------------------------------
  function barRow(name, width, value, muted, tipV, tipL) {
    return '<div class="n" title="' + esc(name) + '">' + esc(name) + '</div>'
      + '<div class="t"' + tipAttrs(tipV, tipL) + '><div class="b' + (muted ? ' muted' : '') + '" style="width:'
      + width + '%"></div><span class="v">' + esc(value) + '</span></div>';
  }

  function renderModels(m) {
    var rows = m.rows.slice(), max = 1;
    rows.forEach(function (r) { max = Math.max(max, r.drones); });
    if (m.other) max = Math.max(max, m.other.drones);
    if (m.unidentified) max = Math.max(max, m.unidentified.drones);
    var w = function (n) { return Math.max(1, Math.round(62 * n / max)); };
    var html = rows.map(function (r) {
      return barRow(r.name, w(r.drones), plural(r.drones, 'drone') + ' · ' + plural(r.flights, 'flight'),
                    false, plural(r.drones, 'drone'), r.name + ' · ' + plural(r.flights, 'flight'));
    }).join('');
    if (m.other) {
      html += barRow('Other (' + plural(m.other.models, 'model') + ')', w(m.other.drones),
                     plural(m.other.drones, 'drone') + ' · ' + plural(m.other.flights, 'flight'),
                     false, plural(m.other.drones, 'drone'), plural(m.other.models, 'less common model'));
    }
    if (m.unidentified) {
      html += barRow('Not identified', w(m.unidentified.drones),
                     plural(m.unidentified.drones, 'drone') + ' · ' + plural(m.unidentified.flights, 'flight'),
                     true, plural(m.unidentified.drones, 'drone'),
                     'no FAA declaration found, not looked up yet, or no serial');
    }
    el('models').innerHTML = html || '<div class="an-empty">No flights match these filters.</div>';
  }

  function renderRadio(r) {
    if (!r || r.error) { el('radio').innerHTML = '<div class="an-empty">Could not load.</div>'; return; }
    var max = 1;
    r.rows.forEach(function (x) { max = Math.max(max, x.detections); });
    if (r.not_reported) max = Math.max(max, r.not_reported.detections);
    var w = function (n) { return Math.max(1, Math.round(62 * n / max)); };
    var html = r.rows.map(function (x) {
      return barRow(x.label, w(x.detections), fmtInt(x.detections) + ' · ' + plural(x.drones, 'drone'),
                    false, plural(x.detections, 'detection'), x.label + ' · ' + plural(x.drones, 'drone'));
    }).join('');
    if (r.not_reported) {
      html += barRow('Not reported', w(r.not_reported.detections), fmtInt(r.not_reported.detections),
                     true, plural(r.not_reported.detections, 'detection'),
                     'from firmware that did not report band and channel');
    }
    el('radio').innerHTML = html || '<div class="an-empty">No detections match these filters.</div>';
  }

  // -- refresh ---------------------------------------------------------------------------------
  function refresh() {
    var my = ++gen, q = params().toString();
    saveUrl();
    state.all = false;
    el('an').classList.add('stale');
    el('fStatus').textContent = 'loading…';
    fetch('/api/analysis/overview?' + q).then(json).then(function (d) {
      if (my !== gen) return;
      el('an').classList.remove('stale');
      if (d.error) { el('fStatus').textContent = d.error; return; }
      state.data = d;
      renderTiles(d.tiles);
      renderHeat(d.heatmap);
      renderGroups(d.groups);
      renderDrones();
      renderModels(d.models);
      el('fStatus').textContent = d.tiles.flights ? '' : 'no flights match these filters';
    }).catch(function () {
      if (my === gen) { el('an').classList.remove('stale'); el('fStatus').textContent = 'cannot reach the server'; }
    });
    fetch('/api/analysis/radio?' + q).then(json).then(function (r) { if (my === gen) renderRadio(r); })
      .catch(function () { renderRadio(null); });
    fetch('/api/analysis/nodes?' + q).then(json).then(function (r) {
      if (my !== gen) return;
      renderReceivers(r);
      drawReceivers();
    }).catch(function () { renderReceivers(null); });
    loadCells(true);
  }

  ['fGroup', 'fDrone', 'fTag', 'fNode', 'fFrom', 'fTo'].forEach(function (id) {
    el(id).addEventListener('change', refresh);
  });
  el('fRange').addEventListener('change', function () { showCustom(); refresh(); });
  el('fReset').addEventListener('click', function () {
    el('fRange').value = 'all'; el('fGroup').value = ''; el('fDrone').value = ''; el('fTag').value = '';
    el('fNode').value = '';
    el('fFrom').value = ''; el('fTo').value = '';
    showCustom();
    refresh();
  });
  el('hmNums').addEventListener('change', function () { if (state.data) renderHeat(state.data.heatmap); });

  loadOptions().then(function () { loadUrl(); refresh(); });
})();
