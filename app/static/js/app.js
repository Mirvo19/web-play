/* HC CDN Player — shared frontend helpers.
 *
 * Live updates use plain polling (fetch + setInterval), not SSE/WebSocket:
 * job/upload state already lives in the DB behind lightweight JSON
 * endpoints, so at 1-2 s cadence polling is simpler, stateless, and needs
 * no connection management under gunicorn's sync workers.
 *
 * Motion (motion.dev UMD, `window.Motion`) decorates state changes only:
 * entrances, toasts, press feedback. Live data is always written to the DOM
 * first; animation never blocks or delays it. Everything no-ops cleanly when
 * the CDN bundle is unavailable or prefers-reduced-motion is set.
 * All DOM updates use textContent / createElement. No innerHTML sinks.
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

  function svgIcon(name, cls) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', cls || 'ic');
    svg.setAttribute('aria-hidden', 'true');
    var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#' + name);
    svg.appendChild(use);
    return svg;
  }

  /** Start polling fn() every ms; return a stopper. Runs once immediately. */
  function poll(fn, ms) {
    var timer = setInterval(function () {
      try { fn(); } catch (e) { /* keep polling */ }
    }, ms);
    try { fn(); } catch (e) { /* ignore first-run errors */ }
    return function () { clearInterval(timer); };
  }

  /* ---------- motion helpers ---------- */
  function reducedMotion() {
    return window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function motionOK() {
    return !reducedMotion() &&
      typeof window.Motion !== 'undefined' &&
      window.Motion && typeof window.Motion.animate === 'function';
  }

  /** Staggered entrance for [data-enter] children of scope (or document). */
  function enter(scope) {
    var root = scope || document;
    var nodes = root.querySelectorAll('[data-enter]');
    if (!nodes.length || !motionOK()) return;
    try {
      window.Motion.animate(
        nodes,
        { opacity: [0, 1], transform: ['translateY(10px)', 'translateY(0px)'] },
        { duration: 0.3, delay: window.Motion.stagger(0.035), easing: 'ease-out' }
      );
    } catch (e) { /* decorative only */ }
  }

  /** Smoothly tween a textContent number toward `to` (300ms, decorative). */
  function tween(elNode, to, fmt) {
    var format = fmt || function (v) { return String(Math.round(v)); };
    if (!elNode) return;
    if (!motionOK()) { elNode.textContent = format(to); return; }
    var from = elNode._hcVal;
    if (typeof from !== 'number' || isNaN(from)) {
      elNode.textContent = format(to);
      elNode._hcVal = to;
      return;
    }
    elNode._hcVal = to;
    var start = null, dur = 300;
    function frame(ts) {
      if (start === null) start = ts;
      var p = Math.min(1, (ts - start) / dur);
      var eased = 1 - Math.pow(1 - p, 3);
      elNode.textContent = format(from + (to - from) * eased);
      if (p < 1 && elNode._hcVal === to) requestAnimationFrame(frame);
      else elNode.textContent = format(elNode._hcVal);
    }
    requestAnimationFrame(frame);
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
    var x = el('button', { type: 'button', 'aria-label': 'Dismiss notification' });
    x.appendChild(svgIcon('i-x'));
    x.addEventListener('click', function () { dismiss(t); });
    head.appendChild(x);
    t.appendChild(head);
    if (message) t.appendChild(el('div', { className: 'tm' }, message));
    box.appendChild(t);
    if (motionOK()) {
      try {
        window.Motion.animate(
          t,
          { opacity: [0, 1], transform: ['translateX(16px)', 'translateX(0px)'] },
          { duration: 0.22, easing: 'ease-out' }
        );
      } catch (e) { /* decorative */ }
    }
    if (ms !== 0) setTimeout(function () { dismiss(t); }, ms || 5000);
    return t;
  }

  function dismiss(t) {
    if (!t || !t.parentNode) return;
    if (!motionOK()) { t.remove(); return; }
    try {
      var done = false;
      var finish = function () { if (!done) { done = true; t.remove(); } };
      window.Motion.animate(
        t, { opacity: [1, 0], transform: ['translateX(0px)', 'translateX(16px)'] },
        { duration: 0.18, easing: 'ease-in' }
      );
      setTimeout(finish, 220);
    } catch (e) { t.remove(); }
  }

  /* ---------- session countdown (sidebar badge) ---------- */
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
      var label = document.getElementById('sessionBadgeText');
      if (label) label.textContent = pad(h) + ':' + pad(m) + ':' + pad(s);
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

  window.HC = {
    el: el, svgIcon: svgIcon, poll: poll, toast: toast,
    formatBytes: formatBytes, enter: enter, tween: tween,
    motionOK: motionOK, reducedMotion: reducedMotion
  };

  document.addEventListener('DOMContentLoaded', function () {
    applyTheme(currentTheme());
    var btn = document.getElementById('themeToggle');
    if (btn) {
      btn.addEventListener('click', function () {
        applyTheme(currentTheme() === 'dark' ? 'light' : 'dark');
      });
      /* Press micro-interaction: quick scale dip, springs back. */
      if (motionOK()) {
        var press = function () {
          try {
            window.Motion.animate(btn, { transform: ['scale(1)', 'scale(0.96)'] },
              { duration: 0.08, easing: 'ease-in' });
          } catch (e) { /* decorative */ }
        };
        var release = function () {
          try {
            window.Motion.animate(btn, { transform: ['scale(0.96)', 'scale(1)'] },
              { duration: 0.14, easing: 'ease-out' });
          } catch (e) { /* decorative */ }
        };
        btn.addEventListener('pointerdown', press);
        btn.addEventListener('pointerup', release);
        btn.addEventListener('pointerleave', release);
      }
    }
    /* Entrance pass for everything tagged data-enter. */
    enter(document);
    if (document.getElementById('sessionBadge')) {
      updateSession();
      setInterval(updateSession, 5000);
    }
  });

  /* ---------- theme (dark default, stored choice wins) ---------- */
  function currentTheme() {
    var t = document.documentElement.getAttribute('data-theme');
    return t === 'light' ? 'light' : 'dark';
  }
  function applyTheme(t) {
    if (t !== 'dark' && t !== 'light') t = 'dark';
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('hc-theme', t); } catch (e) { /* ignore */ }
    var label = document.getElementById('themeLabel');
    if (label) label.textContent = t === 'dark' ? 'Light mode' : 'Dark mode';
    var icon = document.getElementById('themeIcon');
    if (icon) icon.setAttribute('href', t === 'dark' ? '#i-moon' : '#i-sun');
  }
})();
