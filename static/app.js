/* PoriotCloud Vault — client-side behavior */

// ---------------- toast (ad-gate feedback) ----------------
function showToast(html) {
  var t = document.createElement('div');
  t.className = 'toast';
  t.innerHTML = html;
  document.body.appendChild(t);
  requestAnimationFrame(function () {
    requestAnimationFrame(function () { t.classList.add('show'); });
  });
  setTimeout(function () {
    t.classList.remove('show');
    setTimeout(function () { t.remove(); }, 400);
  }, 4000);
}

// ---------------- ad gate + download (works on vault pages) ----------------
(function () {
  var gate = document.body.getAttribute('data-ad-gate') || 'off';
  var adLink = document.body.getAttribute('data-ad-link') || '';
  if (!gate || gate === 'off') return;

  // Popunder: Adsterra's own format — fires on page load, never blocks the user
  if (gate === 'popunder') {
    var ps = document.getElementById('popunder');
    if (ps) {
      var code = ps.textContent.trim();
      var tmp = document.createElement('div');
      tmp.innerHTML = code;
      var injected = false;
      tmp.querySelectorAll('script').forEach(function (s) {
        var ns = document.createElement('script');
        if (s.src) ns.src = s.src; else ns.text = s.text;
        document.head.appendChild(ns);
        injected = true;
      });
      if (!injected && /^https?:/.test(code)) {
        var ns2 = document.createElement('script');
        ns2.src = code;
        document.head.appendChild(ns2);
      }
    }
    return;
  }

  // Redirect gate: first click opens the ad in a new tab, user returns and
  // clicks again — then the action runs. (sessionStorage remembers per visit)
  window.__gateCheck = function () {
    if (sessionStorage.getItem('pv_gate_ok')) return true;
    if (!adLink) return true; // no link configured — never block
    window.open(adLink, '_blank');
    sessionStorage.setItem('pv_gate_ok', '1');
    showToast('Ad opened in a new tab — come back and click <b>again</b> to copy/download');
    return false;
  };

  function rawJson() {
    var cfg = document.getElementById('cfg');
    if (!cfg) return null;
    try { return JSON.parse(cfg.textContent.trim()); } catch (e) { return null; }
  }

  var dlBtn = document.getElementById('dlBtn');
  if (dlBtn) {
    dlBtn.addEventListener('click', function () {
      if (window.__gateCheck && !window.__gateCheck()) return;
      var text = rawJson();
      if (!text) return;
      var blob = new Blob([text], { type: 'application/json' });
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = document.body.getAttribute('data-fname') || 'decoded_config.json';
      document.body.appendChild(a);
      a.click();
      a.remove();
      var label = document.getElementById('dlLabel');
      if (label) { label.textContent = 'Downloaded!'; setTimeout(function () { label.textContent = 'Download .json'; }, 1600); }
    });
  }
})();

// ---------------- vault page: syntax highlighting + copy + countdown ----------------
(function () {
  var cfgScript = document.getElementById('cfg');
  if (!cfgScript) return;
  var raw = cfgScript.textContent.trim();           // the JSON text (tojson-escaped string literal)
  var obj;
  try { obj = JSON.parse(JSON.parse(raw)); } catch (e) { return; }  // unescape → parse → object

  // syntax highlight
  var code = document.getElementById('code');
  var html = '', ln = 1;
  var line = function (td) { return '<tr><td class="ln">' + ln++ + '</td><td class="cd">' + td + '</td></tr>'; };
  var esc = function (s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); };
  var fmt = function (v) {
    if (typeof v === 'string') return '<span class="s">"' + esc(v) + '"</span>';
    if (typeof v === 'number') return '<span class="n">' + v + '</span>';
    if (typeof v === 'boolean') return '<span class="b">' + v + '</span>';
    if (v === null) return '<span class="b">null</span>';
    return esc(JSON.stringify(v));
  };
  var pad = function (depth) { return '  '.repeat(depth); };
  function render(value, depth, key) {
    if (value === null || typeof value !== 'object') {
      html += line(pad(depth) + (key !== undefined ? '<span class="' + (String(key).startsWith('_') ? 'sig' : 'k') + '">"' + esc(key) + '"</span><span class="p">: </span>' : '') + fmt(value) + '<span class="p">,</span>');
      return;
    }
    var entries = Object.entries(value);
    html += line(pad(depth) + (key !== undefined ? '<span class="' + (String(key).startsWith('_') ? 'sig' : 'k') + '">"' + esc(key) + '"</span><span class="p">: </span>' : '') + '<span class="p">{</span>');
    entries.forEach(function (e, i) {
      if (e[1] !== null && typeof e[1] === 'object') render(e[1], depth + 1, e[0]);
      else {
        var comma = i < entries.length - 1 ? '<span class="p">,</span>' : '';
        html += line(pad(depth + 1) + '<span class="' + (String(e[0]).startsWith('_') ? 'sig' : 'k') + '">"' + esc(e[0]) + '"</span><span class="p">: </span>' + fmt(e[1]) + comma);
      }
    });
    html += line(pad(depth) + '<span class="p">}</span>' + (depth > 0 ? '<span class="p">,</span>' : ''));
  }
  render(obj, 0);
  code.innerHTML = html;

  // signature timestamp from the JSON
  var sigTime = document.getElementById('sigTime');
  if (sigTime && obj._decoded_at) sigTime.textContent = 'decoded ' + obj._decoded_at;

  // copy button
  var copyBtn = document.getElementById('copyBtn');
  var copyLabel = document.getElementById('copyLabel');
  if (copyBtn && copyLabel) {
    copyBtn.addEventListener('click', function () {
      if (window.__gateCheck && !window.__gateCheck()) return;
      (navigator.clipboard ? navigator.clipboard.writeText(raw) : Promise.reject())
        .catch(function () {
          var ta = document.createElement('textarea');
          ta.value = raw; document.body.appendChild(ta); ta.select();
          document.execCommand('copy'); ta.remove();
        });
      copyBtn.classList.add('copied');
      copyLabel.textContent = 'Copied!';
      setTimeout(function () {
        copyBtn.classList.remove('copied');
        copyLabel.textContent = 'Copy JSON';
      }, 1600);
    });
  }

  // countdown → auto-destroy (defensive: only run if the server gave us time)
  var remaining = Number(window.__REMAINING);
  if (!(remaining > 0)) remaining = 0;
  var cd = document.getElementById('cd');
  var fill = document.getElementById('cdFill');
  if (!cd) return;
  var total = 6 * 3600;
  function tick() {
    remaining = Math.max(0, remaining - 1);
    var h = String(Math.floor(remaining / 3600)).padStart(2, '0');
    var m = String(Math.floor(remaining % 3600 / 60)).padStart(2, '0');
    var s = String(remaining % 60).padStart(2, '0');
    cd.textContent = h + ':' + m + ':' + s;
    if (fill) fill.style.width = (remaining / total * 100) + '%';
    if (remaining <= 0) { location.href = location.pathname; return; }
    setTimeout(tick, 1000);
  }
  tick();
})();

// ---------------- landing page: drag-&-drop decode with animated progress ----------------
(function () {
  var dz = document.getElementById('dropzone');
  if (!dz) return;
  var input = document.getElementById('fileInput');
  var overlay = document.getElementById('decodeOverlay');

  function start(file) {
    if (!file) return;
    if (!/\.dark$/i.test(file.name) && !/dark/i.test(file.name)) {
      showToast('Please choose a <b>.dark</b> file');
      return;
    }
    overlay.hidden = false;
    var fill = document.getElementById('ovFill');
    var pct = document.getElementById('ovPct');
    var stage = document.getElementById('ovStage');
    var title = document.getElementById('ovFile');
    title.textContent = file.name;

    var STAGES = [
      [8, 'Uploading…'], [26, 'Decrypting layer 1 · AES-256-CFB'],
      [51, 'Decrypting layer 2 · MessagePack'], [74, 'Cleaning & signing…'],
      [93, 'Opening vault…']
    ];
    var i = 0;
    function advance() {
      if (i < STAGES.length) {
        var st = STAGES[i++];
        fill.style.width = st[0] + '%';
        pct.textContent = st[0] + '%';
        stage.textContent = st[1];
        if (i <= STAGES.length - 1) setTimeout(advance, 320);
      }
    }
    advance();

    var fd = new FormData();
    fd.append('file', file);
    fetch('/api/decode', { method: 'POST', body: fd })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        fill.style.width = '100%';
        pct.textContent = '100%';
        stage.textContent = res.ok ? '✅ Done!' : '❌ Error';
        if (res.ok) {
          stage.textContent = '✅ Done! Opening vault…';
          setTimeout(function () { location.href = res.d.url; }, 600);
        } else {
          setTimeout(function () {
            overlay.hidden = true;
            showToast('❌ ' + (res.d.detail || 'Could not decode that file'));
          }, 1200);
        }
      })
      .catch(function () {
        overlay.hidden = true;
        showToast('❌ Network error — is the server reachable?');
      });
  }

  dz.addEventListener('click', function () { input.click(); });
  input.addEventListener('change', function () { start(input.files[0]); });
  ['dragover', 'dragenter'].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add('drag'); });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove('drag'); });
  });
  dz.addEventListener('drop', function (e) { start(e.dataTransfer.files[0]); });
})();

// ---------------- admin: segmented placement + toggle + save flash ----------------
(function () {
  document.querySelectorAll('[data-seg]').forEach(function (seg) {
    var hidden = document.getElementById(seg.dataset.seg);
    if (!hidden) return;
    seg.querySelectorAll('button').forEach(function (btn) {
      btn.addEventListener('click', function () {
        seg.querySelectorAll('button').forEach(function (b) { b.classList.remove('on'); });
        btn.classList.add('on');
        hidden.value = btn.dataset.val;
      });
    });
  });

  var saved = document.getElementById('saved');
  if (saved) {
    var params = new URLSearchParams(location.search);
    if (params.get('saved') === '1') {
      saved.classList.add('show');
      setTimeout(function () { saved.classList.remove('show'); }, 2200);
    }
  }
})();
