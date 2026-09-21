/* HC CDN Player — shared frontend helpers.
 *
 * Live updates use plain polling (fetch + setInterval), not SSE/WebSocket.
 * Rationale: job/upload state already lives in the DB behind lightweight
 * JSON endpoints; at a 1–2 s cadence polling is simpler, stateless, and
 * needs no connection management under gunicorn's sync workers — with no
 * perceptible UX difference at this scale.
 *
 * All DOM updates here use textContent / createElement. No innerHTML sinks.
 */
(function () {
  'use strict';

  /** Build an element safely: el('div', {className:'x'}, 'text'). */
  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        if (k === 'className') node.className = attrs[k];
        else node.setAttribute(k, attrs[k]);
      }
    }
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  /** Start polling fn() every ms; return a stopper. Runs once immediately. */
  function poll(fn, ms) {
    var timer = setInterval(function () {
      try { fn(); } catch (e) { /* keep polling */ }
    }, ms);
    try { fn(); } catch (e) { /* ignore first-run errors */ }
    return function () { clearInterval(timer); };
  }

  /* ---------- toasts (safe DOM, no innerHTML) ---------- */
  function ensureToasts() {
    var c = document.getElementById('toasts');
    if (!c) {
      c = el('div', { id: 'toasts', 'aria-live': 'polite' });
      document.body.appendChild(c);
    }
    return c;
  }

  function toast(type, title, message, ms) {
    var box = ensureToasts();
    var t = el('div', { className: 'toast ' + (type || 'info') });
    var head = el('div', { className: 'tt' });
    head.appendChild(el('span', null, title || ''));
    var x = el('button', { type: 'button', 'aria-label': 'Dismiss' }, '×');
    x.addEventListener('click', function () { t.remove(); });
    head.appendChild(x);
    t.appendChild(head);
    if (message) t.appendChild(el('div', { className: 'tm' }, message));
    box.appendChild(t);
    if (ms !== 0) setTimeout(function () { t.remove(); }, ms || 5000);
    return t;
  }

  /* ---------- session countdown (topbar badge) ---------- */
  function pad(n) { return String(n).padStart(2, '0'); }

  function updateSession() {
    var badge = document.getElementById('sessionBadge');
    if (!badge) return;
    fetch('/api/auth/session').then(function (resp) {
      if (resp.status === 401) { window.location.href = '/login'; return null; }
      if (!resp.ok) return null;
      return resp.json();
    }).then(function (data) {
      if (!data || !data.authenticated) return;
      var remaining = data.remaining_seconds || 0;
      if (remaining <= 0) { window.location.href = '/login'; return; }
      var h = Math.floor(remaining / 3600);
      var m = Math.floor((remaining % 3600) / 60);
      var s = Math.floor(remaining % 60);
      badge.textContent = 'session ' + pad(h) + ':' + pad(m) + ':' + pad(s);
      badge.classList.toggle('danger', !!data.warning_3h);
      var banner = document.getElementById('sessionWarning');
      if (banner) {
        banner.style.display = data.warning_3h ? 'flex' : 'none';
        var msg = document.getElementById('sessionWarningText');
        if (msg) msg.textContent = 'Session expires in ' + h + 'h ' + m + 'm. Log in again to continue working.';
      }
    }).catch(function () { /* stay silent on transient errors */ });
  }

  /* ---------- formatting ---------- */
  function formatBytes(bytes, decimals) {
    if (!bytes) return '0 B';
    var k = 1024, dm = decimals === undefined ? 1 : decimals;
    var sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
  }

  window.HC = { el: el, poll: poll, toast: toast, formatBytes: formatBytes };

  /* ---------- theme (dark default, stored choice wins) ---------- */
  function currentTheme() {
    var t = document.documentElement.getAttribute('data-theme');
    return t === 'light' ? 'light' : 'dark';
  }
  function applyTheme(t) {
    if (t !== 'dark' && t !== 'light') t = 'dark';
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('hc-theme', t); } catch (e) { /* ignore */ }
    var btn = document.getElementById('themeToggle');
    if (btn) btn.textContent = t === 'dark' ? 'Light mode' : 'Dark mode';
  }

  document.addEventListener('DOMContentLoaded', function () {
    applyTheme(currentTheme());
    var btn = document.getElementById('themeToggle');
    if (btn) {
      btn.addEventListener('click', function () {
        applyTheme(currentTheme() === 'dark' ? 'light' : 'dark');
      });
    }
    if (document.getElementById('sessionBadge')) {
      updateSession();
      setInterval(updateSession, 5000);
    }
  });
})();
