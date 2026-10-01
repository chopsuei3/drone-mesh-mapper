/* Drones & Groups: the identity surface.
 *
 * Editing a label here attaches it to the DRONE, not a MAC - so it survives the
 * MAC randomization that made the legacy per-MAC aliases silently stop
 * following a drone. The MAC list on each card is the audit trail.
 */
(function () {
  'use strict';
  var el = function (id) { return document.getElementById(id); };
  var TAGS = ['unknown','civilian','police','government','military','commercial','known'];
  var groups = [];

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function when(ts) {
    return ts ? new Date(ts * 1000).toLocaleDateString([], {year:'2-digit',month:'short',day:'2-digit'}) : '-';
  }

  function loadGroups() {
    return fetch('/api/groups').then(function (r) { return r.json(); }).then(function (gs) {
      groups = gs;
      el('groups').innerHTML = gs.length ? gs.map(function (g) {
        return '<div class="card">'
          + '<h3><span class="swatch" style="background:' + esc(g.color || '#4fc3f7') + '"></span>'
          + esc(g.name) + '</h3>'
          + '<div class="kv"><span>drones</span><span>' + g.drone_count + '</span></div>'
          + '<div class="kv"><span>flights</span><span>' + g.flight_count + '</span></div>'
          + '<div style="margin-top:8px"><button data-del="' + g.id + '">Delete</button></div>'
          + '</div>';
      }).join('') : '<div class="dim">No groups yet. Create one above, then assign drones to it.</div>';
    });
  }

  function loadDrones() {
    var q = el('dq').value.trim();
    fetch('/api/drones' + (q ? '?q=' + encodeURIComponent(q) : ''))
      .then(function (r) { return r.json(); })
      .then(function (ds) {
        el('drones').innerHTML = ds.length ? ds.map(function (d) {
          var opts = groups.map(function (g) {
            return '<option value="' + g.id + '"' + (g.id === d.group_id ? ' selected' : '') + '>'
                 + esc(g.name) + '</option>';
          }).join('');
          var tags = TAGS.map(function (t) {
            return '<option' + (t === (d.tag || 'unknown') ? ' selected' : '') + '>' + t + '</option>';
          }).join('');
          return '<div class="card" data-id="' + d.id + '">'
            + '<h3><a href="/drone/' + d.id + '" style="color:inherit">'
            + esc(d.label || d.basic_id || ('drone #' + d.id)) + '</a>'
            + (d.id_type === 'DJI' ? ' <span class="badge" title="heard through DJI DroneID">DJI</span>' : '')
            + '</h3>'
            + '<div class="kv"><span>serial</span><span class="mono">' + esc(d.basic_id || '-') + '</span></div>'
            + (d.faa_make || d.faa_model
                ? '<div class="kv"><span>model</span><span>'
                  + esc([d.faa_make, d.faa_model].filter(Boolean).join(' ')) + '</span></div>' : '')
            + '<div class="kv"><span>flights</span><span>' + d.flight_count + '</span></div>'
            + '<div class="kv"><span>distance</span><span>' + (d.total_distance_m/1000).toFixed(2) + ' km</span></div>'
            + '<div class="kv"><span>MACs seen</span><span>' + d.mac_count + '</span></div>'
            + '<div class="kv"><span>last seen</span><span>' + when(d.last_seen) + '</span></div>'
            + '<div style="margin-top:9px; display:grid; gap:6px">'
            + '<input class="lbl" placeholder="label this drone" value="' + esc(d.label || '') + '">'
            + '<select class="grp"><option value="">- no group -</option>' + opts + '</select>'
            + '<select class="tag">' + tags + '</select>'
            + '<div style="display:flex; gap:6px; align-items:center">'
            // The picker opens on the colour the drone is actually drawn in -
            // chosen, group or automatic - so it always matches the map.
            + '<input type="color" class="col" value="' + esc(d.color || d.display_color) + '"'
            + (d.color ? '' : ' data-unset="1"')
            + ' title="Colour for this drone in the flight table and on the map"'
            + ' style="padding:0; width:44px; height:26px">'
            + '<span class="dim" style="font-size:11.5px">'
            + (d.color ? 'drone colour' : d.group_color ? 'group colour' : 'automatic colour')
            + '</span>'
            + '<button class="nocol" style="margin-left:auto"' + (d.color ? '' : ' disabled')
            + '>Use default</button></div>'
            + '<button class="save primary">Save</button></div>'
            + '</div>';
        }).join('') : '<div class="dim">No drones yet. Import history or start ingesting.</div>';
      });
  }

  el('gadd').addEventListener('click', function () {
    var name = el('gname').value.trim(); if (!name) return;
    fetch('/api/groups', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ name: name, color: el('gcolor').value }) })
      .then(function () { el('gname').value=''; loadGroups().then(loadDrones); });
  });

  el('groups').addEventListener('click', function (e) {
    var id = e.target.getAttribute('data-del'); if (!id) return;
    fetch('/api/groups/' + id, { method:'DELETE' }).then(function () {
      loadGroups().then(loadDrones);
    });
  });

  function patchDrone(id, body) {
    return fetch('/api/drones/' + id, {
      method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
  }

  // Touching the picker is what marks a colour as chosen: saving a label on a
  // drone that has no colour must not quietly give it the picker's default.
  el('drones').addEventListener('input', function (e) {
    if (e.target.classList.contains('col')) delete e.target.dataset.unset;
  });

  el('drones').addEventListener('click', function (e) {
    var card = e.target.closest('.card');
    if (!card) return;
    if (e.target.classList.contains('nocol')) {
      patchDrone(card.dataset.id, { color: null }).then(loadDrones);
      return;
    }
    if (!e.target.classList.contains('save')) return;
    var body = {
      label: card.querySelector('.lbl').value.trim(),
      group_id: card.querySelector('.grp').value || null,
      tag: card.querySelector('.tag').value };
    var col = card.querySelector('.col');
    if (col && !col.dataset.unset) body.color = col.value;
    patchDrone(card.dataset.id, body)
      .then(function () { e.target.textContent = 'Saved'; setTimeout(loadDrones, 400); });
  });

  var t = null;
  el('dq').addEventListener('input', function () { clearTimeout(t); t = setTimeout(loadDrones, 220); });

  loadGroups().then(loadDrones);
})();
