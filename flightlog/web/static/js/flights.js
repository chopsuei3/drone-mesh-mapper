/* Flight table: the home screen.
 *
 * Everything is server-side - filtering, sorting, paging - so the DOM never
 * holds more than one page of rows and query cost does not grow with history.
 * Selection is kept in a Set that survives filter changes, so a selection can
 * be built up across several different queries and then plotted at once.
 */
(function () {
  'use strict';

  var state = {
    sort: 'started_at', order: 'desc',
    limit: 100, offset: 0, total: 0,
    rows: [], selected: new Set(), meta: {}
  };

  var el = function (id) { return document.getElementById(id); };
  var mv = new MapView('map');

  /* ---- formatting ------------------------------------------------------ */
  function fmtTime(ts) {
    if (!ts) return '';
    var d = new Date(ts * 1000);
    return d.toLocaleString([], { year: '2-digit', month: 'short', day: '2-digit',
                                  hour: '2-digit', minute: '2-digit' });
  }
  function fmtDur(s) {
    if (s === null || s === undefined) return '';
    s = Math.round(s);
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60), r = s % 60;
    if (m < 60) return m + 'm ' + (r < 10 ? '0' : '') + r + 's';
    return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
  }
  function fmtDist(m) {
    if (!m) return '0';
    return m < 1000 ? Math.round(m) + ' m' : (m / 1000).toFixed(2) + ' km';
  }
  function fmtCoord(lat, lon) {
    if (lat === null || lat === undefined) return '<span class="dim">-</span>';
    return '<span class="mono">' + lat.toFixed(5) + ', ' + lon.toFixed(5) + '</span>';
  }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  /* ---- query ----------------------------------------------------------- */
  function params() {
    var p = new URLSearchParams();
    p.set('sort', state.sort); p.set('order', state.order);
    p.set('limit', state.limit); p.set('offset', state.offset);
    var q = el('q').value.trim(); if (q) p.set('q', q);
    var tag = el('tag').value; if (tag) p.set('tag', tag);
    var grp = el('group').value; if (grp) p.set('group_id', grp);
    var tr = el('track').value; if (tr !== '') p.set('has_track', tr);
    var f = el('from').value, t = el('to').value;
    if (f) p.set('from', new Date(f + 'T00:00:00').getTime() / 1000);
    if (t) p.set('to', new Date(t + 'T23:59:59').getTime() / 1000);
    return p;
  }

  function load() {
    fetch('/api/flights?' + params().toString())
      .then(function (r) { return r.json(); })
      .then(function (d) {
        state.rows = d.rows; state.total = d.total;
        render();
      });
  }

  /* ---- render ---------------------------------------------------------- */
  function render() {
    var tb = el('rows'), html = '';
    state.rows.forEach(function (f) {
      state.meta[f.id] = f;
      var who = f.drone_label || f.basic_id || f.mac || '?';
      // Resolved on the server - the drone's own colour, else its group's, else
      // its automatic one - and the map's path uses the same value.
      var color = f.display_color || colorFor(f.drone_id);
      var model = [f.faa_make, f.faa_model].filter(Boolean).join(' ');
      var sel = state.selected.has(f.id);
      html += '<tr data-id="' + f.id + '" class="' + (sel ? 'sel ' : '') + (f.open ? 'open' : '') + '">'
        + '<td><input type="checkbox" class="rowsel"' + (sel ? ' checked' : '') + '></td>'
        + '<td>' + fmtTime(f.started_at) + (f.open ? ' <span class="badge live">live</span>' : '') + '</td>'
        + '<td><span class="swatch pick" data-drone="' + f.drone_id + '" data-color="' + esc(color)
        + '" title="Click to change this drone\'s colour" style="cursor:pointer;background:'
        + esc(color) + '"></span>'
        + esc(who)
        + (model ? ' <span class="dim" style="font-size:11.5px">' + esc(model) + '</span>' : '')
        + (f.id_type === 'DJI' ? ' <span class="badge" title="heard through DJI DroneID">DJI</span>' : '')
        + (f.has_track ? '' : ' <span class="badge notrack">no track</span>')
        + (f.suspect_count ? ' <span class="badge suspect" title="'
            + f.suspect_count + ' implausible fixes excluded">' + f.suspect_count + '!</span>' : '')
        + '</td>'
        + '<td class="dim">' + esc(f.group_name || '-') + '</td>'
        + '<td class="num">' + fmtDur(f.duration_s) + '</td>'
        + '<td class="num">' + fmtDist(f.distance_m) + '</td>'
        + '<td class="num">' + (f.max_alt_m !== null ? Math.round(f.max_alt_m) + ' m' : '<span class="dim">-</span>') + '</td>'
        + '<td class="num">' + (f.max_speed_ms !== null ? f.max_speed_ms.toFixed(1) : '<span class="dim">-</span>') + '</td>'
        + '<td class="num">' + f.det_count + '</td>'
        + '<td class="num">' + (f.max_rssi !== null ? f.max_rssi : '') + '</td>'
        + '<td>' + fmtCoord(f.start_lat, f.start_lon) + '</td>'
        + '<td>' + fmtCoord(f.end_lat, f.end_lon) + '</td>'
        + '</tr>';
    });
    tb.innerHTML = html;
    el('empty').style.display = state.rows.length ? 'none' : 'block';

    var from = state.total ? state.offset + 1 : 0;
    var to = Math.min(state.offset + state.limit, state.total);
    el('range').textContent = from + '-' + to + ' of ' + state.total;
    el('prev').disabled = state.offset <= 0;
    el('next').disabled = to >= state.total;

    document.querySelectorAll('thead th[data-sort]').forEach(function (th) {
      var active = th.dataset.sort === state.sort;
      th.innerHTML = th.textContent.replace(/[▲▼]\s*$/, '').trim()
        + (active ? ' <span class="arrow">' + (state.order === 'desc' ? '▼' : '▲') + '</span>' : '');
    });
    updateSelBar();
  }

  function updateSelBar() {
    var n = state.selected.size;
    el('selbar').classList.toggle('hidden', n === 0);
    el('selCount').textContent = n;
    var dist = 0, dur = 0;
    state.selected.forEach(function (id) {
      var f = state.meta[id];
      if (f) { dist += f.distance_m || 0; dur += f.duration_s || 0; }
    });
    el('selSummary').textContent = n ? (fmtDist(dist) + ' flown  ·  ' + fmtDur(dur) + ' total') : '';
  }

  /* ---- selection & plotting ------------------------------------------- */
  function plot(fit) {
    var ids = Array.from(state.selected);
    if (!ids.length) { mv.clear(); el('mapHint').style.display = 'block'; return; }
    fetch('/api/flights/paths?ids=' + ids.join(','))
      .then(function (r) { return r.json(); })
      .then(function (paths) {
        var meta = {};
        Object.keys(paths).forEach(function (id) {
          var f = state.meta[id] || {};
          meta[id] = { popup: '<b>' + esc(f.drone_label || f.basic_id || f.mac || id) + '</b><br>'
            + fmtTime(f.started_at) + '<br>' + fmtDist(f.distance_m) + ' · ' + fmtDur(f.duration_s) };
        });
        mv.setPaths(paths, meta);
        el('mapHint').style.display = Object.keys(paths).length ? 'none' : 'block';
        if (fit) mv.fit();
      });
  }

  /* ---- per-drone colour ------------------------------------------------ */
  // One native colour picker, reused. It is parked over the clicked swatch so
  // the browser opens its colour dialog there rather than in a page corner.
  var picker = document.createElement('input');
  picker.type = 'color';
  picker.style.cssText = 'position:fixed;width:1px;height:1px;opacity:0;border:0;padding:0';
  document.body.appendChild(picker);
  var pickFor = null;

  // The picker only takes #rrggbb; default swatches are hsl(). Convert, so the
  // dialog opens on the colour the drone is already showing.
  function toHex(css) {
    if (/^#[0-9a-f]{6}$/i.test(css)) return css.toLowerCase();
    var m = /hsl\(\s*([\d.]+)\s*,\s*([\d.]+)%\s*,\s*([\d.]+)%/.exec(css || '');
    if (!m) return '#4fc3f7';
    var h = m[1] / 360, s = m[2] / 100, l = m[3] / 100;
    var q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
    function ch(t) {
      t = (t + 1) % 1;
      var v = t < 1 / 6 ? p + (q - p) * 6 * t : t < 1 / 2 ? q
            : t < 2 / 3 ? p + (q - p) * (2 / 3 - t) * 6 : p;
      return ('0' + Math.round(v * 255).toString(16)).slice(-2);
    }
    return '#' + ch(h + 1 / 3) + ch(h) + ch(h - 1 / 3);
  }

  function pickColor(sw) {
    pickFor = Number(sw.dataset.drone);
    var r = sw.getBoundingClientRect();
    picker.style.left = r.left + 'px';
    picker.style.top = r.bottom + 'px';
    picker.value = toHex(sw.dataset.color);
    picker.click();
  }

  // 'change' fires once, when a new colour is committed, so this is one save
  // per pick rather than one per drag step. Reloading the table and the paths
  // recolours every flight of that drone at once.
  picker.addEventListener('change', function () {
    if (pickFor === null) return;
    var id = pickFor;
    pickFor = null;
    fetch('/api/drones/' + id, { method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ color: picker.value }) })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); })
      .then(function () { load(); plot(false); })
      .catch(function () { alert('Could not save the colour.'); });
  });

  /* ---- events ---------------------------------------------------------- */
  el('rows').addEventListener('click', function (e) {
    var tr = e.target.closest('tr'); if (!tr) return;
    if (e.target.classList.contains('pick')) { pickColor(e.target); return; }
    var id = Number(tr.dataset.id);
    if (e.target.classList.contains('rowsel')) {
      if (e.target.checked) state.selected.add(id); else state.selected.delete(id);
      tr.classList.toggle('sel', e.target.checked);
      updateSelBar();
      plot(false);
      return;
    }
    // Clicking the row body focuses that flight, selecting it if needed.
    if (!state.selected.has(id)) {
      state.selected.add(id);
      tr.classList.add('sel');
      var cb = tr.querySelector('.rowsel'); if (cb) cb.checked = true;
      updateSelBar();
      plot(false);
      setTimeout(function () { mv.focus(id); }, 250);
    } else {
      mv.focus(id);
    }
  });

  el('rows').addEventListener('mouseover', function (e) {
    var tr = e.target.closest('tr'); if (tr) mv.highlight(tr.dataset.id, true);
  });
  el('rows').addEventListener('mouseout', function (e) {
    var tr = e.target.closest('tr'); if (tr) mv.highlight(tr.dataset.id, false);
  });

  el('selAll').addEventListener('change', function () {
    state.rows.forEach(function (f) {
      if (this.checked) state.selected.add(f.id); else state.selected.delete(f.id);
    }, this);
    render(); plot(false);
  });

  el('plotSel').addEventListener('click', function () { plot(true); });
  el('fitSel').addEventListener('click', function () { mv.fit(); });
  el('mergeSel').addEventListener('click', function () {
    var ids = Array.from(state.selected);
    if (ids.length < 2) { alert('Select two or more flights of the same drone.'); return; }
    if (!confirm('Merge ' + ids.length + ' flights into one? This cannot be undone.')) return;
    fetch('/api/flights/merge', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ ids: ids }) })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.error) { alert(res.error); return; }
        state.selected.clear(); state.selected.add(res.merged_into);
        load(); plot(true);
      });
  });

  el('clearSel').addEventListener('click', function () {
    state.selected.clear(); render(); mv.clear();
    el('mapHint').style.display = 'block';
  });

  document.querySelectorAll('thead th[data-sort]').forEach(function (th) {
    th.addEventListener('click', function () {
      var s = th.dataset.sort;
      if (state.sort === s) state.order = (state.order === 'desc' ? 'asc' : 'desc');
      else { state.sort = s; state.order = 'desc'; }
      state.offset = 0; load();
    });
  });

  el('prev').addEventListener('click', function () {
    state.offset = Math.max(0, state.offset - state.limit); load();
  });
  el('next').addEventListener('click', function () {
    state.offset += state.limit; load();
  });

  var t = null;
  function reload() { state.offset = 0; clearTimeout(t); t = setTimeout(load, 220); }
  ['q', 'from', 'to', 'tag', 'group', 'track'].forEach(function (id) {
    el(id).addEventListener('input', reload);
    el(id).addEventListener('change', reload);
  });
  el('reset').addEventListener('click', function () {
    ['q', 'from', 'to'].forEach(function (id) { el(id).value = ''; });
    ['tag', 'group', 'track'].forEach(function (id) { el(id).value = ''; });
    reload();
  });

  ['Csv', 'Kml', 'Gpx'].forEach(function (k) {
    el('exp' + k).addEventListener('click', function () {
      var p = params(); p.delete('limit'); p.delete('offset');
      location.href = '/api/export/flights.' + k.toLowerCase() + '?' + p.toString();
    });
  });

  /* ---- boot ------------------------------------------------------------ */
  fetch('/api/groups').then(function (r) { return r.json(); }).then(function (gs) {
    var sel = el('group');
    gs.forEach(function (g) {
      var o = document.createElement('option');
      o.value = g.id; o.textContent = g.name + ' (' + g.drone_count + ')';
      sel.appendChild(o);
    });
  });

  fetch('/api/stats').then(function (r) { return r.json(); }).then(function (s) {
    el('headerStats').innerHTML =
      '<span><b>' + s.flights + '</b> flights</span>' +
      '<span><b>' + s.drones + '</b> drones</span>' +
      '<span><b>' + (s.distance_m / 1000).toFixed(1) + '</b> km logged</span>' +
      (s.open_flights ? '<span style="color:var(--ok)"><b>' + s.open_flights + '</b> live</span>' : '');
  });

  load();
})();
