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
   would reset the scroll position and the port picker. Every line shown here is
   over-the-air data an attacker can shape, so it only ever reaches the DOM as
   text (textContent, createTextNode, new Option) - never through innerHTML. */
(function () {
  'use strict';
  var el = function (id) { return document.getElementById(id); };
  var POLL_MS = 2000;
  var MAX_DOM_LINES = 1000;     // oldest rows are dropped past this
  var TAGS = { detection: 'DET', heartbeat: 'HB ', other: 'MSG', sent: 'TX>' };
  var view = el('rawView'), picker = el('rawPort'), pauseBtn = el('rawPause');
  // Cursor: the `seq` of the newest line shown; the server sends only later
  // ones. Sequence numbers restart with the server process, so the cursor is
  // sent with the `boot` id it came from and a restart is detected, not missed.
  var since = null, boot = null;
  var paused = false, off = false, inflight = false, timer = null;
  var gen = 0;                  // bumped by restart() so a reply already in flight is dropped
  var err = '', knownPorts = null;

  function pad(n, w) { n = String(n); while (n.length < w) n = '0' + n; return n; }
  function clock(t) {
    var d = new Date(t * 1000);
    return pad(d.getHours(), 2) + ':' + pad(d.getMinutes(), 2) + ':'
      + pad(d.getSeconds(), 2) + '.' + pad(d.getMilliseconds(), 3);
  }
  function atBottom() { return view.scrollHeight - view.scrollTop - view.clientHeight < 24; }
  function span(cls, text) {
    var s = document.createElement('span');
    s.className = cls;
    s.textContent = text;
    return s;
  }

  function row(ln, showPort) {
    var kind = TAGS.hasOwnProperty(ln.kind) ? ln.kind : 'other';
    var r = document.createElement('div');
    r.className = 'rl rl-' + kind;
    r.title = new Date(ln.t * 1000).toString()
      + (kind === 'sent' ? '  (sent host → device)' : '');
    r.appendChild(span('rt', clock(ln.t)));
    r.appendChild(document.createTextNode(' '));
    if (showPort) {
      r.appendChild(span('rp', ln.port));
      r.appendChild(document.createTextNode(' '));
    }
    r.appendChild(span('rk', TAGS[kind]));
    r.appendChild(document.createTextNode(' ' + ln.text));
    return r;
  }

  // `note`, when set, becomes a divider row placed ahead of the new lines.
  function append(lines, note) {
    if (!lines.length && !note) return;
    var stick = atBottom();
    var frag = document.createDocumentFragment();
    if (note) {
      var g = document.createElement('div');
      g.className = 'rl rl-gap';
      g.textContent = note;
      frag.appendChild(g);
    }
    var showPort = !picker.value;
    for (var i = 0; i < lines.length; i++) frag.appendChild(row(lines[i], showPort));
    view.appendChild(frag);

    var extra = view.childNodes.length - MAX_DOM_LINES;
    if (extra > 0) {
      // overflow-anchor is off so every browser behaves the same: measure what
      // is about to go (one layout, not one per row) and shift scrollTop by it,
      // so a reader who scrolled up keeps looking at the same lines.
      var lost = stick ? 0 : view.childNodes[extra].offsetTop - view.firstChild.offsetTop;
      while (extra-- > 0) view.removeChild(view.firstChild);
      if (!stick) view.scrollTop -= lost;
    }
    if (stick) view.scrollTop = view.scrollHeight;
  }

  function showState() {
    var s = off ? '' : paused ? 'paused' : err
      || (atBottom() ? 'live' : 'scrolled up - not following new lines');
    if (el('rawState').textContent !== s) el('rawState').textContent = s;
  }

  function showNote(d) {
    el('rawNote').textContent = (d.serial === false
      ? 'This machine reads no serial ports (--no-serial, or pyserial is unavailable); '
        + 'lines from remote nodes still appear. ' : '')
      + (d.log_path
        ? 'Also logged to ' + d.log_path + ' (size-capped, rotated). Follow it: tail -f "'
          + d.log_path + '"'
        : 'File log off (--no-serial-log, or the file could not be opened); only recent '
          + 'lines are kept, in memory.');
  }

  function setPorts(ports) {
    var key = ports.join('\n');
    if (key === knownPorts) return;
    knownPorts = key;
    var cur = picker.value;
    while (picker.options.length > 1) picker.remove(1);
    ports.forEach(function (p) { picker.add(new Option(p, p)); });
    if (cur && ports.indexOf(cur) < 0) picker.add(new Option(cur, cur));
    picker.value = cur;
  }

  function schedule(ms) {
    clearTimeout(timer);
    timer = (paused || off || document.hidden) ? null : setTimeout(poll, ms);
  }

  function restart() {
    gen++;
    inflight = false;
    schedule(0);
    showState();
  }

  function poll() {
    timer = null;
    if (paused || off || document.hidden || inflight) return;
    var my = gen;
    var q = '?limit=500' + (picker.value ? '&port=' + encodeURIComponent(picker.value) : '')
      + (since != null ? '&since=' + since : '')
      + (since != null && boot ? '&boot=' + encodeURIComponent(boot) : '');
    inflight = true;
    fetch('/api/ports/raw' + q)
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) {
        if (my !== gen) return;
        err = '';
        off = !d.available;
        showNote(d);
        setPorts(d.ports || []);
        var lines = d.lines || [];
        // reset: the server restarted since the last poll, so our cursor
        // belonged to another process and it sent its recent window instead.
        var note = d.reset ? '... server restarted - showing its recent lines'
          : (d.more && since != null)
            ? '... lines skipped: more arrived than this page was sent'
            : '';
        if (d.reset) since = null;
        boot = d.boot || null;
        append(lines, note);
        if (lines.length) since = lines[lines.length - 1].seq;
      })
      .catch(function () { if (my === gen) err = 'cannot reach the server, retrying'; })
      .then(function () {
        if (my !== gen) return;
        inflight = false;
        showState();
        schedule(POLL_MS);
      });
  }

  picker.addEventListener('change', function () {
    view.textContent = '';
    since = null;
    restart();
  });
  pauseBtn.addEventListener('click', function () {
    paused = !paused;
    pauseBtn.textContent = paused ? 'Resume' : 'Pause';
    pauseBtn.setAttribute('aria-pressed', String(paused));
    restart();
  });
  el('rawClear').addEventListener('click', function () { view.textContent = ''; showState(); });
  view.addEventListener('scroll', showState);
  // Only poll while the tab is visible; catch up at once when it comes back.
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { clearTimeout(timer); timer = null; } else restart();
  });

  poll();
})();

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
        return '<div class="chan" data-id="' + c.id + '">'
          + '<div class="row"><b>' + esc(c.name) + '</b><span class="badge">' + esc(c.type) + '</span>'
          + '<label style="margin-left:auto"><input type="checkbox" class="ntog"'
          + (c.enabled ? ' checked' : '') + '> enabled</label>'
          + '<button class="ntest">Test</button><button class="ndel">Delete</button></div>'
          + '<div class="sub">' + esc(describe(c.filter)) + ' · cooldown '
          + Math.round(c.cooldown_s / 60) + ' min</div>'
          + '<div class="sub mono">' + esc(target(c)) + '</div>'
          + '<div class="sub nres">last: ' + last + '</div></div>';
      }).join('') : '<div class="dim">No channels yet. Add Discord or Pushbullet below.</div>';
    });
  }

  function loadLog() {
    return fetch('/api/notify/log?limit=15').then(json).then(function (rows) {
      el('nLog').innerHTML = rows.length ? rows.map(function (r) {
        var who = r.drone_id ? (r.drone_label || r.basic_id || droneName(r.drone_id)) : 'test';
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
    msg.style.color = ''; msg.textContent = 'saving...';
    send('POST', '/api/notify/channels', {
      type: type, name: el('nName').value.trim(), config: cfg, filter: filter,
      cooldown_s: isNaN(mins) ? 900 : Math.max(0, mins) * 60
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
    if (!e.target.classList.contains('ntog')) return;
    var id = e.target.closest('.chan').getAttribute('data-id');
    send('PATCH', '/api/notify/channels/' + id, { enabled: e.target.checked }).then(loadChannels);
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
