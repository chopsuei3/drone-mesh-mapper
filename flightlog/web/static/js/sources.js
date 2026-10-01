/* Port selection + ingest health. Polls slowly - this page is not a hot path. */
(function () {
  'use strict';
  var el = function (id) { return document.getElementById(id); };
  function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');}
  function ago(s) {
    s = Math.round(s);
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    return Math.floor(s / 3600) + 'h';
  }
  // The firmware prints a status line about once a minute even with nothing in
  // range, so a connected node silent for well over that is not scanning.
  function heard(age, connected) {
    if (!connected) return '<span class="dim">-</span>';
    if (age == null) return '<span class="badge notrack">no output yet</span>';
    if (age < 150) return '<span class="badge live">alive</span> <span class="dim">'
      + ago(age) + ' ago</span>';
    return '<span class="badge suspect">silent ' + ago(age) + '</span>';
  }

  function render(d) {
    if (!d.available) {
      el('ports').innerHTML = '<div class="card"><b>Serial ingest is off.</b>'
        + '<div class="dim" style="margin-top:6px">Started with <span class="mono">--no-serial</span>, '
        + 'or pyserial is unavailable. HTTP ingest still works.</div></div>';
      return;
    }
    if (!d.ports.length) {
      el('ports').innerHTML = '<div class="card dim">No serial devices detected. '
        + 'Plug the node in &mdash; this list refreshes automatically.</div>';
      return;
    }
    el('ports').innerHTML = d.ports.map(function (p) {
      var on = d.selected.indexOf(p.device) >= 0;
      var live = d.status[p.device];
      var n = d.counts[p.device] || 0;
      var h = d.health || {};
      var lineAge = (h.line_age_s || {})[p.device];
      var detAge = (h.detection_age_s || {})[p.device];
      return '<div class="card">'
        + '<h3><label style="cursor:pointer"><input type="checkbox" class="psel" data-d="'
        + esc(p.device) + '"' + (on ? ' checked' : '') + '> ' + esc(p.device) + '</label></h3>'
        + '<div class="kv"><span>description</span><span>' + esc(p.description) + '</span></div>'
        + '<div class="kv"><span>state</span><span>'
        + (!on ? '<span class="dim">not selected</span>'
              : live ? '<span class="badge live">connected</span>'
                     : '<span class="badge notrack">waiting</span>') + '</span></div>'
        + (on ? '<div class="kv"><span>node</span><span>' + heard(lineAge, live)
              + '</span></div>' : '')
        + '<div class="kv"><span>detections</span><span>' + n
        + (detAge != null ? ' <span class="dim">(last ' + ago(detAge) + ' ago)</span>' : '')
        + '</span></div>'
        + (on ? '<div style="margin-top:8px"><button class="cmd" data-d="' + esc(p.device)
              + '">Send STATUS</button></div>' : '')
        + '</div>';
    }).join('');
  }

  function load() {
    fetch('/api/ports').then(function (r) { return r.json(); }).then(render);
  }

  el('ports').addEventListener('change', function (e) {
    if (!e.target.classList.contains('psel')) return;
    var sel = Array.prototype.slice.call(document.querySelectorAll('.psel'))
      .filter(function (c) { return c.checked; })
      .map(function (c) { return c.dataset.d; });
    fetch('/api/ports', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ ports: sel }) }).then(function(){ setTimeout(load, 600); });
  });

  el('ports').addEventListener('click', function (e) {
    if (!e.target.classList.contains('cmd')) return;
    fetch('/api/ports/send', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ port: e.target.dataset.d, command: 'STATUS' }) })
      .then(function(r){ return r.json(); })
      .then(function(j){ e.target.textContent = j.sent ? 'Sent' : 'Not connected';
                         setTimeout(function(){ e.target.textContent='Send STATUS'; }, 1400); });
  });

  load();
  setInterval(load, 4000);
})();

/* ---- Raw serial output -------------------------------------------------- */
/* Kept outside #ports: that block is rebuilt from innerHTML every 4 s, which
   would reset the scroll position and the source picker. See rawview.js. */
RawView(document.getElementById('raw'));

/* ---- Notifications ------------------------------------------------------ */
(function () {
  'use strict';
  var el = function (id) { return document.getElementById(id); };
  function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');}
  var TAGS = ['unknown','civilian','police','government','military','commercial','known'];
  var names = { drones: {}, groups: {} };

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
  function ago(ts) {
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return Math.round(s) + 's ago';
    if (s < 3600) return Math.round(s / 60) + 'm ago';
    if (s < 86400) return Math.round(s / 3600) + 'h ago';
    return new Date(ts * 1000).toLocaleDateString();
  }
  function droneName(id) { return names.drones[id] || ('drone #' + id); }
  function picked(id, asInt) {
    return Array.prototype.filter.call(el(id).options, function (o) { return o.selected; })
      .map(function (o) { return asInt ? parseInt(o.value, 10) : o.value; });
  }

  function describe(f) {
    if (f.mode === 'only') {
      var parts = [];
      (f.drone_ids || []).forEach(function (id) { parts.push(droneName(id)); });
      (f.group_ids || []).forEach(function (id) { parts.push('group ' + (names.groups[id] || '#' + id)); });
      (f.tags || []).forEach(function (t) { parts.push('tag ' + t); });
      return 'only ' + parts.join(', ');
    }
    var ex = (f.exclude_drone_ids || []).map(droneName);
    return 'any drone' + (ex.length ? ' except ' + ex.join(', ') : '');
  }
  // Secrets arrive masked from the server; this only shows which one is set.
  function target(c) {
    var cfg = c.config || {};
    if (c.type === 'discord') return cfg.webhook_url + (cfg.mention ? ' · ' + cfg.mention : '');
    return 'token ' + (cfg.access_token || '')
      + (cfg.device_iden ? ' · device ' + cfg.device_iden : '')
      + (cfg.channel_tag ? ' · channel ' + cfg.channel_tag : '');
  }

  function loadPickers() {
    return Promise.all([fetch('/api/drones').then(json), fetch('/api/groups').then(json)])
      .then(function (res) {
        var ds = res[0] || [], gs = res[1] || [];
        names = { drones: {}, groups: {} };
        var opts = ds.map(function (d) {
          var n = d.label || d.basic_id || ('drone #' + d.id);
          var model = [d.faa_make, d.faa_model].filter(Boolean).join(' ');
          names.drones[d.id] = n;
          return '<option value="' + d.id + '">' + esc(n + (model ? ' - ' + model : '')) + '</option>';
        }).join('');
        el('nExclude').innerHTML = opts;
        el('nDrones').innerHTML = opts;
        el('nGroups').innerHTML = gs.map(function (g) {
          names.groups[g.id] = g.name;
          return '<option value="' + g.id + '">' + esc(g.name) + '</option>';
        }).join('');
        el('nTags').innerHTML = TAGS.map(function (t) { return '<option>' + t + '</option>'; }).join('');
      });
  }

  function loadChannels() {
    return fetch('/api/notify/channels').then(json).then(function (cs) {
      el('nChannels').innerHTML = cs.length ? cs.map(function (c) {
        var last = c.last
          ? '<span class="st-' + esc(c.last.status) + '">' + esc(c.last.status) + '</span> '
            + ago(c.last.ts) + (c.last.status === 'failed' ? ' - ' + esc(c.last.detail) : '')
          : 'nothing sent yet';
        var ev = c.events || ['takeoff'];
        return '<div class="chan" data-id="' + c.id + '">'
          + '<div class="row"><b>' + esc(c.name) + '</b><span class="badge">' + esc(c.type) + '</span>'
          + '<label style="margin-left:auto"><input type="checkbox" class="ntog"'
          + (c.enabled ? ' checked' : '') + '> enabled</label>'
          + '<button class="ntest">Test</button><button class="ndel">Delete</button></div>'
          + '<div class="row sub">send '
          + '<label><input type="checkbox" class="nev" data-ev="takeoff"'
          + (ev.indexOf('takeoff') >= 0 ? ' checked' : '') + '> takeoffs</label>'
          + '<label><input type="checkbox" class="nev" data-ev="nodes"'
          + (ev.indexOf('nodes') >= 0 ? ' checked' : '') + '> node problems</label></div>'
          + '<div class="sub">' + (ev.indexOf('takeoff') >= 0 ? esc(describe(c.filter)) + ' · cooldown '
          + Math.round(c.cooldown_s / 60) + ' min' : 'no takeoff alerts') + '</div>'
          + '<div class="sub mono">' + esc(target(c)) + '</div>'
          + '<div class="sub nres">last: ' + last + '</div></div>';
      }).join('') : '<div class="dim">No channels yet. Add Discord or Pushbullet below.</div>';
    });
  }

  function loadLog() {
    return fetch('/api/notify/log?limit=15').then(json).then(function (rows) {
      el('nLog').innerHTML = rows.length ? rows.map(function (r) {
        var who = r.drone_id ? (r.drone_label || r.basic_id || droneName(r.drone_id))
          : r.event ? 'node ' + (r.node_name || '#' + r.node_id) + ': ' + r.event.replace('_', ' ')
          : 'test';
        return '<div class="kv"><span>' + ago(r.ts) + ' · '
          + esc(r.channel_name || 'deleted channel') + ' · ' + esc(who) + '</span>'
          + '<span class="st-' + esc(r.status) + '">' + esc(r.status)
          + (r.detail && r.status !== 'sent' ? ' <span class="dim">' + esc(r.detail) + '</span>' : '')
          + '</span></div>';
      }).join('') : '&mdash;';
    });
  }

  function loadSettings() {
    return fetch('/api/settings').then(json).then(function (s) {
      el('setNotify').checked = !!s['notify.enabled'];
      el('setFaa').checked = !!s['faa.auto'];
      el('setBase').value = s['notify.base_url'] || '';
      el('setBase').placeholder = 'link base for alerts, e.g. ' + location.origin;
    });
  }
  function saveSetting(body) {
    return send('PATCH', '/api/settings', body).then(function (j) {
      el('setMsg').textContent = j.error || 'Saved.';
      el('setMsg').style.color = j.error ? 'var(--bad)' : '';
      loadSettings();
    });
  }

  function showType() {
    var t = el('nType').value;
    Array.prototype.forEach.call(document.querySelectorAll('.ncard [data-for]'), function (n) {
      n.hidden = n.getAttribute('data-for') !== t;
    });
  }
  function showMode() {
    var only = el('nMode').value === 'only';
    el('nPickOnly').hidden = !only;
    el('nPickAny').hidden = only;
  }
  el('nType').addEventListener('change', showType);
  el('nMode').addEventListener('change', showMode);

  el('nAdd').addEventListener('click', function () {
    var type = el('nType').value;
    var cfg = type === 'discord'
      ? { webhook_url: el('nWebhook').value.trim(), username: el('nUsername').value.trim(),
          mention: el('nMention').value.trim() }
      : { access_token: el('nToken').value.trim(), device_iden: el('nDevice').value.trim(),
          channel_tag: el('nChanTag').value.trim() };
    var filter = el('nMode').value === 'only'
      ? { mode: 'only', drone_ids: picked('nDrones', true), group_ids: picked('nGroups', true),
          tags: picked('nTags') }
      : { mode: 'any', exclude_drone_ids: picked('nExclude', true) };
    var mins = parseFloat(el('nCool').value);
    var msg = el('nMsg');
    var events = [];
    if (el('nEvTakeoff').checked) events.push('takeoff');
    if (el('nEvNodes').checked) events.push('nodes');
    msg.style.color = ''; msg.textContent = 'saving...';
    send('POST', '/api/notify/channels', {
      type: type, name: el('nName').value.trim(), config: cfg, filter: filter,
      cooldown_s: isNaN(mins) ? 900 : Math.max(0, mins) * 60, events: events
    }).then(function (j) {
      if (j.error) { msg.textContent = j.error; msg.style.color = 'var(--bad)'; return; }
      msg.textContent = 'Added - use Test to check it.';
      ['nName', 'nWebhook', 'nUsername', 'nMention', 'nToken', 'nDevice', 'nChanTag']
        .forEach(function (id) { el(id).value = ''; });
      loadChannels();
    });
  });

  el('nChannels').addEventListener('click', function (e) {
    var row = e.target.closest('.chan');
    if (!row) return;
    var id = row.getAttribute('data-id');
    if (e.target.classList.contains('ndel')) {
      if (!confirm('Delete this channel and its delivery history?')) return;
      send('DELETE', '/api/notify/channels/' + id).then(function () { loadChannels(); loadLog(); });
    } else if (e.target.classList.contains('ntest')) {
      var b = e.target;
      b.disabled = true; b.textContent = 'Sending...';
      send('POST', '/api/notify/channels/' + id + '/test').then(function (j) {
        b.disabled = false; b.textContent = 'Test';
        row.querySelector('.nres').innerHTML = j.ok
          ? '<span class="st-sent">test sent</span> - check the app'
          : '<span class="st-failed">test failed</span> - ' + esc(j.detail || j.error);
        loadLog();
      });
    }
  });
  el('nChannels').addEventListener('change', function (e) {
    var row = e.target.closest('.chan');
    if (!row) return;
    var id = row.getAttribute('data-id');
    if (e.target.classList.contains('ntog')) {
      send('PATCH', '/api/notify/channels/' + id, { enabled: e.target.checked }).then(loadChannels);
    } else if (e.target.classList.contains('nev')) {
      var events = Array.prototype.filter.call(row.querySelectorAll('.nev'), function (b) { return b.checked; })
        .map(function (b) { return b.getAttribute('data-ev'); });
      send('PATCH', '/api/notify/channels/' + id, { events: events }).then(function (j) {
        if (j.error) row.querySelector('.nres').innerHTML = '<span class="st-failed">' + esc(j.error) + '</span>';
        else loadChannels();
      });
    }
  });

  el('setNotify').addEventListener('change', function (e) {
    saveSetting({ 'notify.enabled': e.target.checked });
  });
  el('setFaa').addEventListener('change', function (e) {
    saveSetting({ 'faa.auto': e.target.checked });
  });
  el('setBaseSave').addEventListener('click', function () {
    saveSetting({ 'notify.base_url': el('setBase').value.trim() });
  });

  showType();
  showMode();
  loadSettings();
  loadPickers().then(loadChannels).then(loadLog);
  setInterval(function () {
    if (!document.hidden) { loadChannels(); loadLog(); }
  }, 20000);
})();

/* ---- Geofences --------------------------------------------------------- */
(function () {
  'use strict';
  var el = function (id) { return document.getElementById(id); };
  function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;');}

  function load() {
    fetch('/api/geofences').then(function (r) { return r.json(); }).then(function (fs) {
      el('fenceList').innerHTML = fs.length ? fs.map(function (f) {
        var g = f.geometry || {};
        var what = f.type === 'circle'
          ? (g.center || []).map(function (v) { return Number(v).toFixed(4); }).join(', ')
            + '  r=' + Math.round(g.radius_m || 0) + ' m'
          : ((g.points || []).length + ' points');
        return '<div class="kv"><span><span class="swatch" style="background:'
          + esc(f.color || '#ff3333') + '"></span>' + esc(f.name)
          + ' <span class="dim mono">' + esc(what) + '</span></span>'
          + '<span><button data-del="' + f.id + '">delete</button></span></div>';
      }).join('') : '<div class="dim">No fences yet.</div>';
    });
  }

  el('fAdd').addEventListener('click', function () {
    var name = el('fName').value.trim();
    var lat = parseFloat(el('fLat').value), lon = parseFloat(el('fLon').value);
    var r = parseFloat(el('fRad').value);
    if (!name || isNaN(lat) || isNaN(lon) || !(r > 0)) {
      alert('Need a name, a centre lat/lon and a positive radius.'); return;
    }
    fetch('/api/geofences', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ name: name, type: 'circle',
        geometry: { center: [lat, lon], radius_m: r } }) })
      .then(function (res) { return res.json(); })
      .then(function (j) {
        if (j.error) { alert(j.error); return; }
        el('fName').value = ''; load();
      });
  });

  el('fenceList').addEventListener('click', function (e) {
    var id = e.target.getAttribute('data-del');
    if (!id) return;
    fetch('/api/geofences/' + id, { method: 'DELETE' }).then(load);
  });

  load();
})();

/* ---- Database maintenance ---------------------------------------------- */
(function () {
  'use strict';
  function mb(b) { return (b / 1048576).toFixed(1) + ' MB'; }
  function load() {
    fetch('/api/maintenance').then(function (r) { return r.json(); }).then(function (s) {
      document.getElementById('maintCard').innerHTML =
        '<div class="kv"><span>file</span><span class="mono">' + s.path + '</span></div>'
        + '<div class="kv"><span>size</span><span>' + mb(s.bytes) + '</span></div>'
        + '<div class="kv"><span>flights</span><span>' + s.flights + '</span></div>'
        + '<div class="kv"><span>detections</span><span>' + s.detections + '</span></div>'
        + '<div class="kv"><span>drones</span><span>' + s.drones + '</span></div>'
        + '<div class="kv"><span>retention</span><span>'
        + (s.retention_days ? s.retention_days + ' days' : 'keep everything') + '</span></div>'
        + (s.thinned_flights ? '<div class="kv"><span>thinned flights</span><span>'
            + s.thinned_flights + ' (summary + path kept)</span></div>' : '')
        + '<div style="margin-top:8px"><button id="vac">Vacuum</button></div>';
      var v = document.getElementById('vac');
      if (v) v.onclick = function () {
        v.textContent = 'working...';
        fetch('/api/maintenance/vacuum', { method: 'POST' }).then(load);
      };
    });
  }
  load();
})();
