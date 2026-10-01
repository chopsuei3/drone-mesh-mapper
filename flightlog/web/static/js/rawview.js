/* Raw serial output: the lines every receiver prints, live, with a source picker.
 *
 * Shared by the Sources and Nodes pages. Sources are named node/port -
 * home/ttyACM0 for this machine's XIAO, north/ttyACM0 for a relay's - so one
 * view covers every XIAO feeding this server.
 *
 * Every line shown here is over-the-air data an attacker can shape, so it only
 * ever reaches the DOM as text (textContent, createTextNode, new Option) - never
 * through innerHTML.
 */
(function (global) {
  'use strict';
  var POLL_MS = 2000;
  var MAX_DOM_LINES = 1000;     // oldest rows are dropped past this
  var TAGS = { detection: 'DET', heartbeat: 'HB ', other: 'MSG', sent: 'TX>' };

  function pad(n, w) { n = String(n); while (n.length < w) n = '0' + n; return n; }
  function clock(t) {
    var d = new Date(t * 1000);
    return pad(d.getHours(), 2) + ':' + pad(d.getMinutes(), 2) + ':'
      + pad(d.getSeconds(), 2) + '.' + pad(d.getMilliseconds(), 3);
  }
  function make(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function RawView(root) {
    var bar = make('div', 'rawbar');
    var label = make('label', 'dim', 'source');
    var picker = make('select');
    picker.add(new Option('all sources', ''));
    picker.id = 'rawPort' + (RawView._n = (RawView._n || 0) + 1);
    picker.setAttribute('aria-label', 'source');
    label.htmlFor = picker.id;
    var pauseBtn = make('button', null, 'Pause');
    pauseBtn.setAttribute('aria-pressed', 'false');
    var clearBtn = make('button', null, 'Clear view');
    var stateEl = make('span', 'dim rawstate');
    var legend = make('span', 'spacer dim mono');
    [['DET', 'var(--ok)', ' detection'], ['HB', null, ' heartbeat/status'],
     ['MSG', 'var(--warn)', ' other'], ['TX>', 'var(--accent)', ' sent host → device']]
      .forEach(function (l, i) {
        var s = make('span', null, l[0]);
        if (l[1]) s.style.color = l[1];
        legend.appendChild(s);
        legend.appendChild(document.createTextNode(l[2] + (i < 3 ? '  ' : '')));
      });
    [label, picker, pauseBtn, clearBtn, stateEl, legend].forEach(function (e) { bar.appendChild(e); });
    var view = make('div', 'rawview mono');
    view.setAttribute('role', 'log');
    view.setAttribute('aria-live', 'off');
    view.tabIndex = 0;
    var note = make('div', 'rawnote dim mono');
    root.appendChild(bar);
    root.appendChild(view);
    root.appendChild(note);

    // Cursor: the `seq` of the newest line shown; the server sends only later
    // ones. Sequence numbers restart with the server process, so the cursor is
    // sent with the `boot` id it came from and a restart is detected, not missed.
    var since = null, boot = null;
    var paused = false, inflight = false, timer = null;
    var gen = 0;                  // bumped by restart() so a reply already in flight is dropped
    var err = '', knownPorts = null;

    function atBottom() { return view.scrollHeight - view.scrollTop - view.clientHeight < 24; }

    function row(ln, showPort) {
      var kind = TAGS.hasOwnProperty(ln.kind) ? ln.kind : 'other';
      var r = make('div', 'rl rl-' + kind);
      r.title = new Date(ln.t * 1000).toString()
        + (kind === 'sent' ? '  (sent host → device)' : '');
      r.appendChild(make('span', 'rt', clock(ln.t)));
      r.appendChild(document.createTextNode(' '));
      if (showPort) {
        r.appendChild(make('span', 'rp', ln.port));
        r.appendChild(document.createTextNode(' '));
      }
      r.appendChild(make('span', 'rk', TAGS[kind]));
      r.appendChild(document.createTextNode(' ' + ln.text));
      return r;
    }

    // `gap`, when set, becomes a divider row placed ahead of the new lines.
    function append(lines, gap) {
      if (!lines.length && !gap) return;
      var stick = atBottom();
      var frag = document.createDocumentFragment();
      if (gap) frag.appendChild(make('div', 'rl rl-gap', gap));
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
      var s = paused ? 'paused' : err
        || (atBottom() ? 'live' : 'scrolled up - not following new lines');
      if (stateEl.textContent !== s) stateEl.textContent = s;
    }

    function showNote(d) {
      note.textContent = (d.serial === false
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
      timer = (paused || document.hidden) ? null : setTimeout(poll, ms);
    }

    function restart() {
      gen++;
      inflight = false;
      schedule(0);
      showState();
    }

    function poll() {
      timer = null;
      if (paused || document.hidden || inflight) return;
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
          showNote(d);
          setPorts(d.ports || []);
          var lines = d.lines || [];
          // reset: the server restarted since the last poll, so our cursor
          // belonged to another process and it sent its recent window instead.
          var gap = d.reset ? '... server restarted - showing its recent lines'
            : (d.more && since != null)
              ? '... lines skipped: more arrived than this page was sent'
              : '';
          if (d.reset) since = null;
          boot = d.boot || null;
          append(lines, gap);
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

    function choose(source) {
      if (source && !Array.prototype.some.call(picker.options, function (o) { return o.value === source; })) {
        picker.add(new Option(source, source));
      }
      picker.value = source || '';
      view.textContent = '';
      since = null;
      restart();
    }

    picker.addEventListener('change', function () { choose(picker.value); });
    pauseBtn.addEventListener('click', function () {
      paused = !paused;
      pauseBtn.textContent = paused ? 'Resume' : 'Pause';
      pauseBtn.setAttribute('aria-pressed', String(paused));
      restart();
    });
    clearBtn.addEventListener('click', function () { view.textContent = ''; showState(); });
    view.addEventListener('scroll', showState);
    // Only poll while the tab is visible; catch up at once when it comes back.
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) { clearTimeout(timer); timer = null; } else restart();
    });

    poll();
    return { choose: choose, element: root };
  }

  global.RawView = RawView;
})(window);
