/* Per-drone history: every flight this airframe has flown, all on one map.
 *
 * The MAC list is the audit trail for identity - it shows every address this
 * drone has broadcast from, which is what makes a durable label defensible
 * rather than a guess.
 */
(function () {
  'use strict';
  var id = window.DRONE_ID, mv = new MapView('map');

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;');
  }
  function km(m) { return (m / 1000).toFixed(2) + ' km'; }
  function dur(s) {
    if (s === null || s === undefined) return '-';
    s = Math.round(s);
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60);
    if (m < 60) return m + 'm ' + (s % 60) + 's';
    return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
  }
  function when(ts) {
    return ts ? new Date(ts * 1000).toLocaleString([], {
      year: '2-digit', month: 'short', day: '2-digit',
      hour: '2-digit', minute: '2-digit' }) : '-';
  }

  // What the serial says about itself, then what the FAA's declaration database
  // says about it. The database identifies the model, never the owner.
  function identCard(d, note) {
    var f = d.faa || {}, sf = d.serial_format || {};
    function kv(k, v, cls) {
      return v ? '<div class="kv"><span>' + k + '</span><span' + (cls ? ' class="' + cls + '"' : '')
        + '>' + esc(v) + '</span></div>' : '';
    }
    var html = '<div class="card"><h3>Identification</h3>';
    if (d.id_type === 'DJI') {
      // DJI's proprietary DroneID: its serial is DJI's own and its operator
      // position is the takeoff point, so neither the FAA lookup nor "pilot" apply.
      return html + kv('heard via', 'DJI DroneID (Wi-Fi beacon)')
        + kv('serial', d.basic_id ? 'DJI serial, not a Remote ID serial' : 'not broadcast')
        + kv('FAA', 'not applicable')
        + '<div class="dim" style="margin-top:6px; font-size:11.5px">The FAA database holds Remote ID'
        + ' serials only; this one is the serial DJI DroneID carries. The operator position shown for this drone is its takeoff'
        + ' (home) point, which DJI DroneID reports instead of a live pilot position.</div></div>';
    }
    if (!d.basic_id) {
      return html + '<div class="dim">No Remote ID serial seen yet, so there is nothing to look up.</div></div>';
    }
    html += kv('format', sf.well_formed
      ? 'standard serial, maker code ' + sf.mfr_code
      : 'not a standard serial (possibly a session ID)');
    if (f.status === 'match') {
      var upd = f.updated_at && !isNaN(Date.parse(f.updated_at))
        ? new Date(f.updated_at).toLocaleDateString() : f.updated_at;
      html += kv('make', f.make) + kv('model', f.model) + kv('series', f.series)
        + kv('compliance', f.compliance) + kv('DOC', f.tracking_number, 'mono')
        + kv('FAA updated', upd);
    } else {
      html += kv('FAA', {
        no_match: 'not in the FAA declaration database',
        error: 'lookup failed - retried automatically',
        unchecked: 'not looked up yet'
      }[f.status] || f.status);
    }
    html += kv('checked', f.checked_at ? when(f.checked_at) : null)
      + (note ? '<div class="dim" style="margin-top:6px; font-size:11.5px">' + esc(note) + '</div>' : '')
      + '<div style="margin-top:8px"><button id="faaAgain">'
      + (f.status === 'unchecked' ? 'Look up now' : 'Look up again') + '</button></div>'
      + '<div class="dim" style="margin-top:6px; font-size:11.5px">'
      + 'Owner details are not public - the FAA shares them only with law enforcement.</div></div>';
    return html;
  }

  function loadSummary(lookupNote) {
    return fetch('/api/drones/' + id + '/summary')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        document.getElementById('dtitle').textContent = d.basic_id || ('drone #' + d.id);
        var macs = (d.macs || []).map(function (m) {
          return '<div class="kv"><span class="mono">' + esc(m.mac) + '</span>'
               + '<span class="dim">' + when(m.last_seen) + '</span></div>';
        }).join('') || '<div class="dim">none</div>';
        var note = (d.macs || []).length > 1
          ? '<div class="dim" style="margin-top:6px; font-size:11.5px">'
            + 'Randomized MACs &mdash; the label follows the serial, not these.</div>' : '';
        document.getElementById('summary').innerHTML =
          '<div class="card"><h3>' + esc(d.label || d.basic_id || ('drone #' + d.id)) + '</h3>'
          + '<div class="kv"><span>serial</span><span class="mono">' + esc(d.basic_id || '-') + '</span></div>'
          + '<div class="kv"><span>group</span><span>' + esc(d.group_name || '-') + '</span></div>'
          + '<div class="kv"><span>tag</span><span>' + esc(d.tag || 'unknown') + '</span></div></div>'
          + '<div class="card"><h3>Lifetime</h3>'
          + '<div class="kv"><span>flights</span><span>' + d.flights + '</span></div>'
          + '<div class="kv"><span>distance</span><span>' + km(d.distance_m) + '</span></div>'
          + '<div class="kv"><span>airtime</span><span>' + dur(d.airtime_s) + '</span></div>'
          + '<div class="kv"><span>max alt</span><span>'
          + (d.max_alt_m !== null ? Math.round(d.max_alt_m) + ' m MSL' : '-') + '</span></div>'
          + '<div class="kv"><span>detections</span><span>' + d.detections + '</span></div></div>'
          + '<div class="card"><h3>MACs used (' + (d.macs || []).length + ')</h3>' + macs + note + '</div>'
          + identCard(d, lookupNote);
      });
  }
  loadSummary();

  document.getElementById('summary').addEventListener('click', function (e) {
    if (e.target.id !== 'faaAgain') return;
    e.target.disabled = true;
    e.target.textContent = 'Looking up...';
    fetch('/api/drones/' + id + '/faa', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (res) { loadSummary(res.error ? 'Lookup failed: ' + res.error : null); })
      .catch(function () { loadSummary('Lookup failed: no response'); });
  });

  fetch('/api/flights?drone_id=' + id + '&limit=500')
    .then(function (r) { return r.json(); })
    .then(function (d) {
      document.getElementById('rows').innerHTML = d.rows.map(function (f) {
        return '<tr data-id="' + f.id + '"><td>' + when(f.started_at) + '</td>'
          + '<td class="num">' + dur(f.duration_s) + '</td>'
          + '<td class="num">' + Math.round(f.distance_m) + ' m</td>'
          + '<td class="num">' + (f.max_alt_m !== null ? Math.round(f.max_alt_m) + ' m' : '-') + '</td>'
          + '<td class="num">' + f.det_count + '</td>'
          + '<td class="mono dim">' + esc(f.mac) + '</td></tr>';
      }).join('');
      var ids = d.rows.map(function (f) { return f.id; });
      if (!ids.length) return;
      fetch('/api/flights/paths?ids=' + ids.join(','))
        .then(function (r) { return r.json(); })
        .then(function (paths) {
          mv.setPaths(paths, {});
          mv.fit();
          document.getElementById('mapHint').textContent = ids.length + ' flights';
        });
    });

  document.getElementById('rows').addEventListener('click', function (e) {
    var tr = e.target.closest('tr');
    if (tr) mv.focus(tr.dataset.id);
  });
})();
