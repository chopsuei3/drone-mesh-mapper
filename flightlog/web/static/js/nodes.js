/* Nodes: every receiver's health, and the controls for remote relays.
 *
 * Polls /api/nodes every few seconds. Relays report their own host, version
 * and the XIAO's status line, so everything a node sends is escaped before it
 * reaches the page; node names are validated server-side but escaped anyway.
 */
(function () {
  'use strict';
  var el = function (id) { return document.getElementById(id); };
  var LABEL = MapView.NODE_STATUS;
  var BAD_RESETS = ['panic', 'int_watchdog', 'task_watchdog', 'watchdog', 'brownout'];
  var POLL_MS = 5000;
  var state = { nodes: [], sel: null, placing: null, reveal: null, shown: null, fitted: false };

  var mv = new MapView('map');
  var raw = RawView(el('raw'));

  // -- helpers -------------------------------------------------------------------
  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/&/g, '&amp;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function json(r) {
    return r.json().then(function (j) {
      if (!r.ok && j && !j.error) j.error = 'HTTP ' + r.status;
      return j;
    });
  }
  function send(method, url, body) {
    return fetch(url, { method: method, headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body) }).then(json);
  }
  function dur(s) {
    s = Math.max(0, Math.round(s || 0));
    if (s < 90) return s + ' s';
    if (s < 5400) return Math.round(s / 60) + ' min';
    if (s < 172800) return Math.floor(s / 3600) + ' h ' + Math.round((s % 3600) / 60) + ' min';
    return Math.round(s / 86400) + ' d';
  }
  function ago(ts) { return ts ? dur(Date.now() / 1000 - ts) + ' ago' : 'never'; }
  function short(p) { return String(p).replace(/^\/dev\//, ''); }
  function source(n, port) { return n.name + '/' + short(port); }
  function byId(id) {
    for (var i = 0; i < state.nodes.length; i++) if (state.nodes[i].id === id) return state.nodes[i];
    return null;
  }
  function msg(id, text, bad) {
    var m = el(id);
    if (!m) return;
    m.textContent = text || '';
    m.style.color = bad ? 'var(--bad)' : '';
  }

  // -- the table -------------------------------------------------------------------
  function badge(n) {
    return '<span class="badge st-' + esc(n.status) + '">' + esc(LABEL[n.status] || n.status) + '</span>';
  }

  function statusDetail(n) {
    if (n.status === 'relay_offline') return 'no contact ' + ago(n.last_contact_at);
    if (n.status === 'xiao_silent') return 'no line from the XIAO ' + ago(n.last_line_at);
    if (n.status === 'waiting') return n.kind === 'local' ? 'no serial line yet' : 'no contact yet';
    if (n.status === 'disabled') return 'its batches are refused';
    return n.kind === 'local' ? 'last line ' + ago(n.last_line_at) : 'checked in ' + ago(n.last_contact_at);
  }

  function relayCell(n) {
    if (n.kind === 'local') return '<span class="dim">this machine</span>';
    var r = n.relay || {};
    if (!r.version) return '<span class="dim">not heard from yet</span>';
    var parts = ['v' + esc(r.version)];
    if (r.uptime_s != null) parts.push('up ' + dur(r.uptime_s));
    parts.push('spool ' + esc(r.spool_depth || 0));
    var html = esc(r.host || '') + '<div class="sub">' + parts.join(' · ') + '</div>';
    if (r.dropped) html += '<div class="sub warnline">' + esc(r.dropped) + ' lines dropped (spool full)</div>';
    if (r.oldest_age_s > 120) {
      html += '<div class="sub warnline">backlog: oldest line ' + dur(r.oldest_age_s) + ' old</div>';
    }
    return html;
  }

  // What the XIAO said in its last status line, per port.
  function xiaoCell(n) {
    var ports = n.ports || [];
    if (!ports.length) return '<span class="dim">no port yet</span>';
    var now = Date.now() / 1000;
    return ports.map(function (p) {
      var x = (n.xiao || {})[p] || {};
      var conn = ((n.relay || {}).ports || {})[p];
      var bits = [];
      if (conn) bits.push(conn.connected ? 'connected' : '<span class="warnline">not connected</span>');
      if (x.reset) {
        bits.push('reset ' + (BAD_RESETS.indexOf(x.reset) >= 0
          ? '<span class="badge suspect" title="the XIAO crashed and restarted">' + esc(x.reset) + '</span>'
          : esc(x.reset)));
      }
      if (x.booted_at) bits.push('up ' + dur(now - x.booted_at));
      else if (x.uptime_s != null && x.t) bits.push('up ' + dur(x.uptime_s + now - x.t));
      if (x.queue_drops) bits.push('<span class="warnline">' + esc(x.queue_drops) + ' queue drops</span>');
      var counters = [['wifi_frames', 'wifi'], ['ble_adv', 'ble'], ['odid_wifi', 'odid wifi'],
                      ['odid_ble', 'odid ble'], ['dji', 'dji'], ['emitted', 'sent'],
                      ['heap', 'heap'], ['channel', 'ch']]
        .filter(function (c) { return x[c[0]] != null; })
        .map(function (c) { return c[1] + ' ' + esc(x[c[0]]); });
      return '<div class="port"><span class="mono">' + esc(short(p)) + '</span> ' + bits.join(' · ')
        + (x.t ? ' <span class="dim">(status ' + ago(x.t) + ')</span>' : '')
        + (counters.length ? '<div class="sub mono">' + counters.join(' · ') + '</div>' : '')
        + '</div>';
    }).join('');
  }

  function clockCell(n) {
    if (n.kind === 'local' || n.clock_offset_s == null) return '<span class="dim">-</span>';
    var off = n.clock_offset_s;
    var txt = (off >= 0 ? '+' : '') + (Math.abs(off) < 10 ? off.toFixed(1) : Math.round(off)) + ' s';
    return Math.abs(off) > 5
      ? '<span class="warnline" title="The Pi\'s clock is off. Harmless: times come from line ages, not its clock.">'
        + txt + '</span>'
      : '<span class="dim">' + txt + '</span>';
  }

  function renderTable() {
    var head = '<thead><tr><th class="nosort">Node</th><th class="nosort">Status</th>'
      + '<th class="nosort">Relay</th><th class="nosort">XIAO</th>'
      + '<th class="nosort">Last detection</th><th class="nosort" title="relay clock minus server clock">Clock</th>'
      + '</tr></thead>';
    var body = state.nodes.map(function (n) {
      return '<tr data-id="' + n.id + '"' + (n.id === state.sel ? ' class="sel"' : '') + '>'
        + '<td><b>' + esc(n.name) + '</b>' + (n.kind === 'local' ? ' <span class="badge">local</span>' : '')
        + (n.lat == null ? '' : ' <span class="dim" title="has a location">◆</span>')
        + (n.notes ? '<div class="sub">' + esc(n.notes) + '</div>' : '') + '</td>'
        + '<td>' + badge(n) + '<div class="sub">' + esc(statusDetail(n)) + '</div></td>'
        + '<td>' + relayCell(n) + '</td>'
        + '<td>' + xiaoCell(n) + '</td>'
        + '<td>' + (n.last_detection_at ? ago(n.last_detection_at) : '<span class="dim">none yet</span>') + '</td>'
        + '<td>' + clockCell(n) + '</td></tr>';
    }).join('');
    el('nodesTable').innerHTML = head + '<tbody>' + body + '</tbody>';
    var online = state.nodes.filter(function (n) { return n.status === 'online'; }).length;
    el('headerStats').innerHTML = '<span><b>' + state.nodes.length + '</b> nodes</span>'
      + '<span' + (online < state.nodes.length ? ' style="color:var(--warn)"' : '') + '><b>'
      + online + '</b> online</span>';
  }

  // -- the selected node -------------------------------------------------------------
  // The form is drawn once per selection so typing is never wiped by a poll;
  // the live parts (status, location, ports, commands) refresh underneath it.
  function renderDetail() {
    var n = byId(state.sel);
    state.shown = n ? n.id + ':' + n.name + ':' + n.enabled + ':' + (n.notes || '') : null;
    if (!n) { el('detail').innerHTML = '<div class="dim">Select a node above.</div>'; return; }
    var relay = n.kind === 'relay';
    el('detail').innerHTML =
      '<h3>' + esc(n.name) + ' <span id="dStatus"></span></h3>'
      + '<div id="dToken"></div>'
      + '<div class="nd-form">'
      + '<div class="row"><input id="dName" class="grow" value="' + esc(n.name) + '" maxlength="32"'
      + ' aria-label="node name"><button id="dRename">Rename</button></div>'
      + '<div class="row"><input id="dNotes" class="grow" placeholder="notes: where it is, who has access"'
      + ' value="' + esc(n.notes || '') + '" maxlength="500" aria-label="notes"><button id="dNotesSave">Save</button></div>'
      + (relay ? '<label><input type="checkbox" id="dEnabled"' + (n.enabled ? ' checked' : '')
        + '> enabled <span class="dim">(when off its batches are refused and it never alerts)</span></label>' : '')
      + '<div class="nd-sep"></div>'
      + '<div class="row"><span class="dim">location</span> <span id="dLoc" class="mono"></span>'
      + '<span class="spacer"></span><button id="dPlace">Place on map</button>'
      + '<button id="dClearLoc">Clear</button></div>'
      + '<div class="nd-sep"></div>'
      + '<div><b>Commands</b></div><div id="dPorts" class="row"></div>'
      + '<div class="dim" style="font-size:11.5px">The dualcore XIAO firmware does not read commands:'
      + ' they reach its port (TX&gt; in the raw view) but get no reply. Node-mode home firmware answers STATUS.</div>'
      + '<div class="nd-cmds" id="dCmds"></div>'
      + '<div class="nd-sep"></div>'
      + '<div class="row"><button id="dRaw">Show raw output</button>'
      + (relay ? '<span class="spacer"></span><button id="dRotate">New token</button>'
        + '<button id="dDelete">Delete node</button>' : '')
      + '</div><span id="dMsg" class="dim"></span></div>';
    if (state.reveal && state.reveal.id === n.id) showToken();
    renderDetailLive();
    loadCommands();
  }

  function renderDetailLive() {
    var n = byId(state.sel);
    if (!n || !el('dStatus')) return;
    el('dStatus').innerHTML = badge(n);
    el('dLoc').textContent = n.lat == null ? 'not set' : n.lat.toFixed(5) + ', ' + n.lon.toFixed(5);
    el('dPlace').textContent = state.placing === n.id ? 'Click the map… (cancel)' : 'Place on map';
    el('dClearLoc').disabled = n.lat == null;
    var ports = n.ports || [];
    el('dPorts').innerHTML = ports.length ? ports.map(function (p) {
      return '<span class="mono">' + esc(short(p)) + '</span>'
        + ['STATUS', 'WATCHDOG_RESET'].map(function (c) {
          return '<button class="cmd" data-port="' + esc(p) + '" data-cmd="' + c + '">' + c + '</button>';
        }).join('');
    }).join('<span style="width:12px"></span>') : '<span class="dim">no port reported yet</span>';
  }

  function cmdState(c) {
    if (c.state === 'queued') return ['st-queued', 'queued for the relay'];
    if (c.state === 'delivered') return ['st-delivered', 'delivered to the relay'];
    return [c.ok ? 'st-done-ok' : 'st-done-bad', c.result || (c.ok ? 'done' : 'failed')];
  }

  function loadCommands() {
    var id = state.sel;
    if (id == null || !el('dCmds')) return;
    fetch('/api/nodes/' + id + '/commands?limit=6').then(json).then(function (cs) {
      if (id !== state.sel || !el('dCmds') || !Array.isArray(cs)) return;
      el('dCmds').innerHTML = cs.map(function (c) {
        var s = cmdState(c);
        return '<div class="kv"><span>' + ago(c.created_at) + ' · ' + esc(c.command) + ' → '
          + esc(short(c.port || '')) + '</span><span class="' + s[0] + '">' + esc(s[1]) + '</span></div>';
      }).join('');
    }).catch(function () {});
  }

  // -- tokens and the install command ----------------------------------------------------
  function serverUrl() {
    return (el('serverUrl').value || location.origin).trim().replace(/\/+$/, '');
  }
  function installCmd(name, token) {
    return 'python3 RPI/install_relay.py --server ' + serverUrl() + ' --token ' + token + ' --name ' + name;
  }
  function showToken() {
    var r = state.reveal, box = r && el('dToken');
    if (!box) return;
    box.innerHTML = '<div class="tokenbox"><b>Token for ' + esc(r.name) + '</b>'
      + ' <span class="warnline">&mdash; shown only now.</span> Copy the command; a lost token'
      + ' is replaced with <i>New token</i>.'
      + '<div class="dim" style="margin-top:6px">On the remote Pi, in the directory holding'
      + ' <span class="mono">flightlog/</span> and <span class="mono">RPI/</span>:</div>'
      + '<pre class="mono" id="cmdText"></pre>'
      + '<button id="cmdCopy">Copy command</button> <span id="cmdMsg" class="dim"></span></div>';
    el('cmdText').textContent = installCmd(r.name, r.token);
    box.scrollIntoView({ block: 'nearest' });
  }

  // -- loading ----------------------------------------------------------------------------
  function drawMap() {
    mv.setNodes(state.nodes, { selected: state.sel, labels: true,
                               onClick: function (n) { select(n.id); } });
    var located = state.nodes.filter(function (n) { return n.lat != null; });
    var hint = el('mapHint');
    var placing = byId(state.placing);
    hint.textContent = placing ? 'Click the map where ' + placing.name + ' is'
      : located.length ? located.length + ' of ' + state.nodes.length + ' nodes placed'
      : 'No node has a location yet: select one, then Place on map';
    el('mapBox').classList.toggle('placing', !!placing);
    if (!state.fitted && located.length) {
      state.fitted = true;
      mv._autoCenter = false;
      if (located.length === 1) mv.map.setView([located[0].lat, located[0].lon], 13);
      else mv.map.fitBounds(L.latLngBounds(located.map(function (n) { return [n.lat, n.lon]; })),
                            { padding: [40, 40], maxZoom: 14 });
    }
  }

  function load() {
    return fetch('/api/nodes').then(json).then(function (ns) {
      if (!Array.isArray(ns)) return;
      state.nodes = ns;
      if (state.sel != null && !byId(state.sel)) state.sel = null;
      renderTable();
      var n = byId(state.sel);
      var key = n ? n.id + ':' + n.name + ':' + n.enabled + ':' + (n.notes || '') : null;
      if (key !== state.shown) renderDetail(); else { renderDetailLive(); loadCommands(); }
      drawMap();
    }).catch(function () {});
  }

  function select(id) {
    state.sel = id;
    state.placing = null;
    if (state.reveal && state.reveal.id !== id) state.reveal = null;   // shown once, for its node
    try { history.replaceState(null, '', '#' + id); } catch (e) { /* file: or sandbox */ }
    renderTable();
    renderDetail();
    drawMap();
  }

  // -- events -------------------------------------------------------------------------------
  el('nodesTable').addEventListener('click', function (e) {
    var tr = e.target.closest('tr[data-id]');
    if (tr) select(Number(tr.getAttribute('data-id')));
  });

  function patch(body, okText) {
    var id = state.sel;
    return send('PATCH', '/api/nodes/' + id, body).then(function (j) {
      msg('dMsg', j.error || okText, !!j.error);
      return load();
    });
  }

  el('detail').addEventListener('click', function (e) {
    var t = e.target, n = byId(state.sel);
    if (!n || t.tagName !== 'BUTTON') return;
    if (t.id === 'dRename') patch({ name: el('dName').value.trim() }, 'Renamed.');
    else if (t.id === 'dNotesSave') patch({ notes: el('dNotes').value.trim() }, 'Saved.');
    else if (t.id === 'dPlace') {
      state.placing = state.placing === n.id ? null : n.id;
      renderDetailLive();
      drawMap();
    } else if (t.id === 'dClearLoc') patch({ lat: null, lon: null }, 'Location cleared.');
    else if (t.id === 'dRaw') {
      raw.choose(n.ports && n.ports.length ? source(n, n.ports[0]) : '');
      el('raw').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else if (t.id === 'dRotate') {
      if (!confirm('Make a new token for ' + n.name + '? The current one stops working at once,'
                   + ' so the relay needs the new one installed.')) return;
      send('POST', '/api/nodes/' + n.id + '/token').then(function (j) {
        if (j.error) { msg('dMsg', j.error, true); return; }
        state.reveal = { id: n.id, name: n.name, token: j.token };
        showToken();
      });
    } else if (t.id === 'dDelete') {
      if (!confirm('Delete ' + n.name + '? Its token stops working and the record of which'
                   + ' flights it heard is removed. Flights and detections stay.')) return;
      send('DELETE', '/api/nodes/' + n.id).then(function (j) {
        if (j.error) { msg('dMsg', j.error, true); return; }
        state.sel = null;
        state.reveal = null;
        load();
      });
    } else if (t.classList.contains('cmd')) {
      t.disabled = true;
      send('POST', '/api/nodes/' + n.id + '/commands',
           { port: t.getAttribute('data-port'), command: t.getAttribute('data-cmd') })
        .then(function (j) {
          t.disabled = false;
          msg('dMsg', j.error || '', !!j.error);
          loadCommands();
        });
    }
  });

  el('detail').addEventListener('change', function (e) {
    if (e.target.id === 'dEnabled') {
      patch({ enabled: e.target.checked }, e.target.checked ? 'Enabled.' : 'Disabled: its batches are refused.');
    }
  });

  document.addEventListener('click', function (e) {
    if (e.target.id !== 'cmdCopy') return;
    var text = el('cmdText').textContent;
    var done = function (ok) { el('cmdMsg').textContent = ok ? 'copied' : 'select the text and copy it'; };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
    } else {
      var range = document.createRange();
      range.selectNodeContents(el('cmdText'));
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
      done(ok);
    }
  });

  mv.map.on('click', function (e) {
    var id = state.placing;
    if (id == null) return;
    state.placing = null;
    send('PATCH', '/api/nodes/' + id, { lat: +e.latlng.lat.toFixed(6), lon: +e.latlng.lng.toFixed(6) })
      .then(function (j) { msg('dMsg', j.error || 'Location set.', !!j.error); load(); });
  });

  el('newAdd').addEventListener('click', function () {
    var name = el('newName').value.trim();
    msg('newMsg', 'creating…');
    send('POST', '/api/nodes', { name: name }).then(function (j) {
      if (j.error) { msg('newMsg', j.error, true); return; }
      msg('newMsg', 'Created ' + j.node.name + ' - its token is shown above, once.');
      el('newName').value = '';
      state.reveal = { id: j.node.id, name: j.node.name, token: j.token };
      state.sel = j.node.id;
      load();
    });
  });
  el('newName').addEventListener('keydown', function (e) { if (e.key === 'Enter') el('newAdd').click(); });

  el('serverUrl').addEventListener('input', function () {
    if (state.reveal && el('cmdText')) el('cmdText').textContent = installCmd(state.reveal.name, state.reveal.token);
  });
  el('serverSave').addEventListener('click', function () {
    send('PATCH', '/api/settings', { 'nodes.server_url': el('serverUrl').value.trim() }).then(function (j) {
      msg('newMsg', j.error || 'Saved: install commands use ' + serverUrl() + '.', !!j.error);
    });
  });

  el('offSave').addEventListener('click', function () {
    var mins = parseFloat(el('offMin').value);
    send('PATCH', '/api/settings', { 'nodes.offline_after_s': isNaN(mins) ? -1 : mins * 60 })
      .then(function (j) {
        msg('offMsg', j.error ? j.error.replace('nodes.offline_after_s', 'the threshold') : 'Saved.', !!j.error);
        if (!j.error) load();
      });
  });

  // -- boot -----------------------------------------------------------------------------------
  fetch('/api/settings').then(json).then(function (s) {
    el('serverUrl').value = (s && s['nodes.server_url']) || location.origin;
    el('serverUrl').placeholder = location.origin;
    el('offMin').value = Math.round((s['nodes.offline_after_s'] || 600) / 60);
  }).catch(function () { el('serverUrl').value = location.origin; });

  var fromHash = parseInt((location.hash || '').slice(1), 10);
  if (!isNaN(fromHash)) state.sel = fromHash;
  load();
  setInterval(function () { if (!document.hidden) load(); }, POLL_MS);
})();
