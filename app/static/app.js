'use strict';

/* ---------- helpers ---------- */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function clampInt(value, min, max, fallback) {
  const n = parseInt(value, 10);
  if (Number.isNaN(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

function maskClient(id) {
  if (!id) return '\u2014';
  if (id.length <= 8) return '\u2022'.repeat(4) + id.slice(-4);
  return id.slice(0, 8) + '\u2022'.repeat(3);
}

function toast(msg, ok) {
  const el = document.createElement('div');
  el.className = 'toast ' + (ok ? 'toast-ok' : 'toast-bad');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

/* ---------- static config ---------- */

const ALGOS = {
  fixed_window: { label: 'Fixed Window', accent: '#4f8cff' },
  sliding_window: { label: 'Sliding Window', accent: '#9d7bff' },
  token_bucket: { label: 'Token Bucket', accent: '#2ecc71' },
  leaky_bucket: { label: 'Leaky Bucket', accent: '#ff9f43' },
};

const ALGO_NOTES = {
  fixed_window: 'A counter that fully resets at the end of each fixed time window.',
  sliding_window: 'A continuous window \u2014 each request expires on its own schedule.',
  token_bucket: 'Tokens refill at a steady rate; a request needs one token.',
  leaky_bucket: 'A FIFO queue that drains at a constant rate; a full bucket rejects.',
};

const ROUTE_METHODS = {
  '/api/test': 'GET',
  '/api/login': 'POST',
  '/api/products': 'GET',
  '/api/orders': 'POST',
};

/* ---------- state ---------- */

const sliding = { dots: new Map(), tl: null };
const leaky = { chips: new Map(), vessel: null, capacity: 10, seq: 1, shift: 0 };

const state = {
  mode: 'simulation',
  algorithm: 'fixed_window',
  backend: 'memory',
  limit: 10,
  window: 60,
  clientId: 'client-1',
  route: '/api/test',
  apiBase: '',
  server: null,
  sessionId: null,
  lastSnapshot: null,
  lastSnapshotNow: 0,
  lastServerNow: 0,
  clockOffset: 0,
  metrics: { requests: 0, allowed: 0, rejected: 0, remaining: 0, limit: 0, reset: 0, rate: 0, success_pct: 0 },
  fixedWindowIndex: 1,
  lastReset: null,
  autoTimer: null,
  sending: false,
  liveStarted: 0,
  liveRequests: 0,
  liveAllowed: 0,
  liveRejected: 0,
  lastLive: null,
  lastEvent: null,
  lastRejected: false,
  vizAlgo: null,
};

function activeAlgorithm() {
  if (state.mode === 'live') return state.server ? state.server.algorithm : state.algorithm;
  return state.algorithm;
}

function serverNow() {
  return (performance.now() - state.clockOffset) / 1000;
}

function readConfig() {
  state.limit = clampInt($('#limit').value, 1, 1000, 10);
  state.window = clampInt($('#window').value, 1, 3600, 60);
  state.clientId = $('#client-id').value.trim() || 'client-1';
  state.route = $('#route').value;
  state.apiBase = $('#api-base').value.trim();
}

/* ---------- boot ---------- */

async function init() {
  bindEvents();
  await loadServerConfig();
  setBackendNote();
  await createSession();
  renderAll();
  startAnimation();
}

/* ---------- server config ---------- */

async function loadServerConfig() {
  try {
    const res = await fetch('/playground/api/config');
    if (!res.ok) throw new Error('config failed');
    state.server = await res.json();
  } catch {
    state.server = null;
  }
  renderServerConfig();
}

function renderServerConfig() {
  const s = state.server;
  const info = $('#server-info');

  if (!s) {
    info.className = 'pill pill-bad';
    info.textContent = 'server unreachable';
  } else {
    info.className = 'pill pill-ok';
    info.textContent = 'v' + s.version + ' \u00b7 ' + s.algorithm.replace(/_/g, ' ');
  }

  $('#lc-algorithm').textContent = s ? s.algorithm : '\u2014';
  $('#lc-backend').textContent = s ? s.backend : '\u2014';
  $('#lc-limit').textContent = s ? String(s.limit) : '\u2014';
  $('#lc-window').textContent = s ? s.window + 's' : '\u2014';
  $('#lc-route-limit').textContent = s ? routeLimitText(s) : '\u2014';
  $('#lc-redis').textContent = s ? (s.redis.available ? 'available' : 'unavailable') : '\u2014';

  renderRedisBadge();
}

function routeLimitText(s) {
  const cfg = s.route_limits && s.route_limits[state.route];
  if (cfg) return cfg.limit + ' req / ' + cfg.window + 's';
  return 'global ' + s.limit + ' req / ' + s.window + 's';
}

function renderRedisBadge() {
  const b = $('#redis-badge');
  const s = state.server;

  if (!s) {
    b.className = 'pill';
    b.textContent = 'Redis \u2026';
    return;
  }

  if (s.redis.available) {
    b.className = 'pill pill-ok';
    b.textContent = 'Redis available';
  } else {
    b.className = 'pill pill-bad';
    b.textContent = 'Redis unavailable';
  }
}

function setBackendNote() {
  const el = $('#backend-note');

  if (state.mode === 'live') {
    el.textContent = 'Live API uses the server backend (' + (state.server ? state.server.backend : 'unknown') + ').';
    return;
  }

  if (state.backend === 'memory') {
    el.textContent = 'Runs the real in-memory algorithms. No Redis needed.';
    return;
  }

  const ok = state.server ? state.server.redis.available : false;
  el.textContent = ok
    ? 'Uses the real Redis-backed algorithms against your local Redis.'
    : 'Redis unavailable \u2014 requests will fail (no memory fallback).';
}

/* ---------- simulation session ---------- */

async function createSession() {
  if (state.mode !== 'simulation') return;

  readConfig();

  const body = {
    algorithm: state.algorithm,
    limit: state.limit,
    window: state.window,
    backend: state.backend,
    client_id: state.clientId,
    route: state.route,
  };

  let res;
  try {
    res = await fetch('/playground/sim/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch {
    toast('Simulation endpoint unreachable \u2014 is RateGuard running?', false);
    return;
  }

  if (!res.ok) {
    handleSimError(res);
    return;
  }

  const data = await res.json();
  state.sessionId = data.session_id;
  state.metrics = Object.assign({}, data.metrics);
  state.lastSnapshot = data.state;
  state.clockOffset = performance.now() - data.state.now * 1000;
  state.lastSnapshotNow = data.state.now;
  state.lastServerNow = data.state.now;
  state.fixedWindowIndex = 1;
  state.lastReset = null;
  state.lastRejected = false;
  clearEvents();
  renderAll();
}

async function sendSim(count) {
  if (!state.sessionId) await createSession();
  if (!state.sessionId) return;

  let res = await fetch('/playground/sim/request', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: state.sessionId, count }),
  });

  if (res.status === 404) {
    await createSession();
    if (!state.sessionId) return;
    res = await fetch('/playground/sim/request', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.sessionId, count }),
    });
  }

  if (!res.ok) {
    handleSimError(res);
    return;
  }

  const data = await res.json();
  ingest(data.events, data.state, data.metrics);
}

async function resetSim() {
  if (!state.sessionId) return;

  const res = await fetch('/playground/sim/reset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: state.sessionId }),
  });

  if (!res.ok) {
    handleSimError(res);
    return;
  }

  const data = await res.json();
  state.sessionId = data.session_id;
  state.metrics = Object.assign({}, data.metrics);
  state.lastSnapshot = data.state;
  state.clockOffset = performance.now() - data.state.now * 1000;
  state.lastSnapshotNow = data.state.now;
  state.lastServerNow = data.state.now;
  state.fixedWindowIndex = 1;
  state.lastReset = null;
  state.lastRejected = false;
  clearEvents();
  renderAll();
}

/* ---------- live API mode ---------- */

function liveMetrics() {
  const elapsed = state.liveStarted ? (performance.now() - state.liveStarted) / 1000 : 0;
  return {
    requests: state.liveRequests,
    allowed: state.liveAllowed,
    rejected: state.liveRejected,
    remaining: state.lastLive ? state.lastLive.remaining : 0,
    limit: state.lastLive ? state.lastLive.limit : 0,
    reset: state.lastLive ? state.lastLive.reset : 0,
    rate: elapsed > 0 ? state.liveRequests / elapsed : 0,
    success_pct: state.liveRequests ? (state.liveAllowed / state.liveRequests) * 100 : 0,
  };
}

async function sendLive(count) {
  readConfig();

  const base = state.apiBase.replace(/\/+$/, '');
  const route = state.route;
  const method = ROUTE_METHODS[route] || 'GET';
  const headers = {};
  if (state.clientId) headers['X-API-Key'] = state.clientId;

  if (!state.liveStarted) state.liveStarted = performance.now();

  for (let i = 0; i < count; i++) {
    let res;
    try {
      res = await fetch(base + route, { method, headers });
    } catch {
      toast('Live API unreachable at ' + (base || 'same origin') + ' \u2014 is RateGuard running?', false);
      return;
    }

    const allowed = res.status === 200;
    const getH = (name) => res.headers.get(name);
    const limitRaw = getH('X-RateLimit-Limit');
    const remainingRaw = getH('X-RateLimit-Remaining');
    const resetRaw = getH('X-RateLimit-Reset');
    const retryRaw = getH('Retry-After');

    const limit = limitRaw != null ? parseInt(limitRaw, 10) : (state.server ? state.server.limit : 0);
    const remaining = remainingRaw != null ? parseInt(remainingRaw, 10) : 0;
    const reset = resetRaw != null ? parseInt(resetRaw, 10) : 0;
    const retryAfter = retryRaw != null ? parseInt(retryRaw, 10) : null;

    const ev = {
      ts: Date.now(),
      allowed,
      status: res.status,
      remaining,
      reset,
      limit,
      retry_after: retryAfter,
      route,
      client: state.clientId,
    };

    state.liveRequests += 1;
    if (allowed) state.liveAllowed += 1;
    else state.liveRejected += 1;

    ingestLiveSnapshot(ev);
    state.metrics = liveMetrics();
    addEventToLog(ev, 'live');
    onIngest([ev]);

    if (activeAlgorithm() === 'sliding_window') {
      slidingAddDot(Date.now() / 1000, allowed, 'live-' + ev.ts);
    }
    if (activeAlgorithm() === 'leaky_bucket' && allowed) {
      leakyAddChip('live-' + ev.ts, Date.now() / 1000, leaky.chips.size);
    }

    renderMetrics();
    renderStateReadout();
    renderViz(state.lastSnapshot);

    if (count > 1) await sleep(30);
  }

  pulseFlow(state.lastEvent ? state.lastEvent.allowed : true);
}

function ingestLiveSnapshot(ev) {
  const algo = activeAlgorithm();
  const limit = ev.limit;
  const window = state.server ? state.server.window : 60;
  const remaining = ev.remaining;
  const reset = ev.reset;
  const now = Date.now() / 1000;

  const base = {
    algorithm: algo,
    backend: 'live',
    limit,
    window,
    remaining,
    reset,
    now,
  };

  if (algo === 'fixed_window') {
    base.used = Math.max(0, limit - remaining);
    base.window_elapsed = Math.max(0, window - reset);
  } else if (algo === 'token_bucket') {
    base.capacity = limit;
    base.refill_rate = limit / window;
    base.tokens = remaining;
  } else if (algo === 'leaky_bucket') {
    base.capacity = limit;
    base.leak_rate = limit / window;
  }

  state.lastSnapshot = base;
  state.clockOffset = performance.now() - now * 1000;
  state.lastSnapshotNow = now;
  state.lastServerNow = now;
  state.lastLive = { remaining, limit, reset };
}

function ingestLiveInitial() {
  if (!state.server) return;

  const algo = activeAlgorithm();
  const limit = state.server.limit;
  const window = state.server.window;
  const now = Date.now() / 1000;

  const base = {
    algorithm: algo,
    backend: 'live',
    limit,
    window,
    remaining: limit,
    reset: window,
    now,
  };

  if (algo === 'fixed_window') {
    base.used = 0;
    base.window_elapsed = 0;
  } else if (algo === 'token_bucket') {
    base.capacity = limit;
    base.refill_rate = limit / window;
    base.tokens = limit;
  } else if (algo === 'leaky_bucket') {
    base.capacity = limit;
    base.leak_rate = limit / window;
  }

  state.lastSnapshot = base;
  state.clockOffset = performance.now() - now * 1000;
  state.lastSnapshotNow = now;
  state.lastServerNow = now;
}

/* ---------- shared ingest / render ---------- */

function ingest(events, snap, metrics) {
  for (const ev of events) addEventToLog(ev, state.mode);
  state.lastSnapshot = snap;
  state.metrics = Object.assign({}, metrics);
  state.clockOffset = performance.now() - snap.now * 1000;
  state.lastSnapshotNow = snap.now;
  state.lastServerNow = snap.now;
  onIngest(events);
  renderAll();
  pulseFlow(events.length ? events[events.length - 1].allowed : true);
}

function onIngest(events) {
  const last = events.length ? events[events.length - 1] : null;
  state.lastEvent = last;
  state.lastRejected = !!last && !last.allowed;

  if (state.lastRejected) {
    const algo = activeAlgorithm();
    if (algo === 'token_bucket') triggerReject($('#tb-vessel'), 'shake');
    if (algo === 'leaky_bucket') triggerReject($('#lb-vessel'), 'full-shake');
  }
}

function renderAll() {
  updateVizTitle();
  renderMetrics();
  renderStateReadout();

  const snap = state.lastSnapshot;
  if (snap) {
    renderViz(snap);
  } else {
    const container = $('#viz-container');
    container.dataset.kind = '';
    container.innerHTML = '<div class="empty-log">No requests yet. Send traffic to see the algorithm in action.</div>';
  }
}

function updateVizTitle() {
  const algo = activeAlgorithm();
  $('#viz-title').textContent = ALGOS[algo].label;
  $('#algo-note').textContent = ALGO_NOTES[algo];

  if (state.vizAlgo !== algo) {
    $('#viz-container').dataset.kind = '';
    state.vizAlgo = algo;
  }
}

function renderMetrics() {
  const m = state.metrics;
  $('#m-requests').textContent = m.requests;
  $('#m-allowed').textContent = m.allowed;
  $('#m-rejected').textContent = m.rejected;
  $('#m-remaining').textContent = m.remaining;
  $('#m-limit').textContent = m.limit;
  $('#m-reset').textContent = m.reset;
  $('#m-rate').textContent = (m.rate || 0).toFixed(1);
  $('#m-success').textContent = (m.success_pct || 0).toFixed(1) + '%';
}

function renderStateReadout() {
  const snap = state.lastSnapshot;
  const el = $('#state-readout');

  if (!snap) {
    el.innerHTML = '';
    return;
  }

  const items = [
    ['Algorithm', snap.algorithm],
    ['Backend', snap.backend],
    ['Limit', snap.limit],
    ['Window', snap.window + 's'],
    ['Remaining', snap.remaining],
    ['Reset', snap.reset + 's'],
  ];

  if (snap.used != null) items.push(['Used', snap.used]);
  if (snap.tokens != null) items.push(['Tokens', snap.tokens.toFixed(2)]);
  if (Array.isArray(snap.timestamps)) items.push(['In window/queue', snap.timestamps.length]);
  if (snap.refill_rate != null) items.push(['Refill rate', snap.refill_rate.toFixed(3) + ' /s']);
  if (snap.leak_rate != null) items.push(['Leak rate', snap.leak_rate.toFixed(3) + ' /s']);

  el.innerHTML = items
    .map(([k, v]) => '<div class="state-item"><span class="k">' + k + '</span><span class="v">' + escapeHtml(String(v)) + '</span></div>')
    .join('');
}

function renderViz(snap) {
  if (!snap) return;

  if (snap.algorithm === 'fixed_window') detectFixedReset(snap);

  switch (snap.algorithm) {
    case 'fixed_window': renderFixed(snap); break;
    case 'sliding_window': renderSliding(snap); break;
    case 'token_bucket': renderToken(snap); break;
    case 'leaky_bucket': renderLeaky(snap); break;
  }
}

/* ---------- event log ---------- */

function addEventToLog(ev, source) {
  const container = $('#event-list');
  const empty = container.querySelector('.empty-log');
  if (empty) empty.remove();

  const el = document.createElement('div');
  el.className = 'event ' + (ev.allowed ? 'ok' : 'bad');

  const d = new Date(ev.ts);
  const time = d.toLocaleTimeString('en-US', { hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0');

  const clientDisplay = source === 'live' ? maskClient(ev.client || state.clientId) : (ev.client || state.clientId || '\u2014');
  const limitVal = ev.limit != null ? ev.limit : state.metrics.limit;
  const retryVal = ev.retry_after != null ? ev.retry_after : ev.reset;

  let html =
    '<div class="ev-top">' +
    '<span class="ev-status">' + (ev.allowed ? '\u2713 ' + ev.status : '\u2717 ' + ev.status) + '</span>' +
    '<span class="ev-time">' + time + '</span>' +
    '</div>' +
    '<div class="ev-meta">' +
    '<span>route <b>' + escapeHtml(ev.route || state.route) + '</b></span>' +
    '<span>client <b>' + escapeHtml(clientDisplay) + '</b></span>' +
    '<span>remaining <b>' + ev.remaining + '</b></span>' +
    '<span>reset <b>' + ev.reset + 's</b></span>' +
    '</div>';

  if (ev.status === 429) {
    html +=
      '<div class="ev-detail">Retry-After ' + retryVal + 's \u00b7 X-RateLimit-Limit ' + limitVal +
      ' \u00b7 X-RateLimit-Remaining 0 \u00b7 X-RateLimit-Reset ' + ev.reset + 's</div>';
  }

  el.innerHTML = html;
  container.prepend(el);

  while (container.children.length > 300) container.lastChild.remove();
  $('#log-count').textContent = container.children.length;

  flashEventStatus(ev.allowed);
}

function flashEventStatus(allowed) {
  const v = $('#viz-container');
  v.classList.remove('flash-ok', 'flash-bad');
  void v.offsetWidth;
  v.classList.add(allowed ? 'flash-ok' : 'flash-bad');
}

function clearEvents() {
  const list = $('#event-list');
  list.innerHTML = '<div class="empty-log">No requests yet. Send traffic to see it here.</div>';
  $('#log-count').textContent = '0';
}

/* ---------- flow pulse ---------- */

function pulseFlow(allowed) {
  if (REDUCED) return;
  const spans = $$('.flow-arrow span');
  if (spans.length < 4) return;
  const [c, m, l, r] = spans;
  spans.forEach((s) => s.classList.remove('hot', 'done-ok', 'done-bad'));
  c.classList.add('hot');
  setTimeout(() => m.classList.add('hot'), 120);
  setTimeout(() => l.classList.add('hot'), 240);
  setTimeout(() => {
    c.classList.remove('hot');
    m.classList.remove('hot');
    l.classList.remove('hot');
    r.classList.add(allowed ? 'done-ok' : 'done-bad');
  }, 360);
}

/* ---------- FIXED WINDOW ---------- */

function buildFixed(snap) {
  const container = $('#viz-container');
  const maxSegs = Math.min(snap.limit, 60);
  let segs = '';
  for (let i = 0; i < maxSegs; i++) segs += '<div class="fw-seg"></div>';

  container.innerHTML =
    '<div class="fw-window">' +
    '<div class="fw-head">' +
    '<span class="fw-label"></span>' +
    '<span class="fw-count"></span>' +
    '</div>' +
    '<div class="fw-progress"><div class="fw-progress-fill"></div></div>' +
    '<div class="fw-segments">' + segs + '</div>' +
    '<div class="fw-foot">' +
    '<span class="fw-state"></span>' +
    '<span class="fw-countdown"></span>' +
    '</div>' +
    '</div>';

  container.dataset.kind = 'fixed_window';
  container.dataset.fixedLimit = String(snap.limit);
}

function updateFixed(snap) {
  const container = $('#viz-container');

  if (container.dataset.kind !== 'fixed_window' || container.dataset.fixedLimit !== String(snap.limit)) {
    buildFixed(snap);
  }

  const used = snap.used != null ? snap.used : Math.max(0, snap.limit - snap.remaining);
  const window = snap.window;
  let reset = Math.max(0, snap.reset);
  if (reset <= 0 && used === 0) reset = window;

  const elapsed = Math.max(0, window - reset);
  const pct = window > 0 ? Math.min(100, (elapsed / window) * 100) : 0;

  container.querySelector('.fw-label').textContent = 'Window #' + state.fixedWindowIndex;
  container.querySelector('.fw-count').textContent = used + ' / ' + snap.limit + ' used';
  container.querySelector('.fw-progress-fill').style.width = pct + '%';
  container.querySelector('.fw-state').textContent =
    used >= snap.limit ? 'window full \u2014 rejections until reset' : 'accepting requests';
  container.querySelector('.fw-countdown').textContent = 'resets in ' + Math.ceil(reset) + 's';

  const segs = container.querySelectorAll('.fw-seg');
  segs.forEach((seg, i) => {
    seg.classList.toggle('used', i < used);
    seg.textContent = i + 1;
  });
}

function renderFixed(snap) {
  updateFixed(snap);
}

function detectFixedReset(snap) {
  const reset = Math.max(0, snap.reset);
  if (state.lastReset != null && reset > state.lastReset + 1) {
    state.fixedWindowIndex += 1;
    const w = document.querySelector('.fw-window');
    if (w) {
      w.classList.add('resetting');
      setTimeout(() => w.classList.remove('resetting'), 900);
    }
  }
  state.lastReset = reset;
}

function animateFixed() {
  const snap = state.lastSnapshot;
  if (!snap || snap.algorithm !== 'fixed_window') return;
  const container = $('#viz-container');
  const fill = container.querySelector('.fw-progress-fill');
  const cd = container.querySelector('.fw-countdown');
  if (!fill || !cd) return;

  const window = snap.window;
  const used = snap.used != null ? snap.used : Math.max(0, snap.limit - snap.remaining);
  let reset0 = Math.max(0, snap.reset);
  if (reset0 <= 0 && used === 0) reset0 = window;

  const reset = Math.max(0, reset0 - Math.max(0, serverNow() - state.lastSnapshotNow));
  const pct = window > 0 ? Math.min(100, ((window - reset) / window) * 100) : 0;

  fill.style.width = pct + '%';
  cd.textContent = 'resets in ' + Math.ceil(reset) + 's';
}

/* ---------- SLIDING WINDOW ---------- */

function buildSliding(snap) {
  const container = $('#viz-container');
  sliding.dots.forEach((d) => d.el.remove());
  sliding.dots.clear();

  container.innerHTML =
    '<div class="sw-timeline">' +
    '<div class="sw-boundary"></div>' +
    '<div class="sw-boundary-label">expires</div>' +
    '</div>' +
    '<div class="sw-axis"><span>now \u2212 ' + snap.window + 's</span><span>now</span></div>' +
    '<div class="sw-caption" id="sw-caption"></div>';

  container.dataset.kind = 'sliding_window';
  sliding.tl = container.querySelector('.sw-timeline');
}

function slidingAddDot(ts, allowed, key) {
  if (sliding.dots.has(key)) return;
  const dot = document.createElement('div');
  dot.className = 'sw-dot' + (allowed ? '' : ' rejected');
  sliding.tl.appendChild(dot);
  sliding.dots.set(key, { ts, allowed, el: dot });
}

function positionSliding() {
  if (!sliding.tl) return;
  const width = sliding.tl.clientWidth;
  const window = (state.lastSnapshot && state.lastSnapshot.window) || state.window;
  const now = serverNow();

  for (const [key, d] of sliding.dots) {
    const age = now - d.ts;
    if (age >= window) {
      d.el.remove();
      sliding.dots.delete(key);
      continue;
    }
    const left = (1 - age / window) * width;
    d.el.style.left = left + 'px';
    d.el.classList.toggle('fading', age > window * 0.8);
  }
}

function renderSliding(snap) {
  const container = $('#viz-container');

  if (container.dataset.kind !== 'sliding_window') buildSliding(snap);

  if (Array.isArray(snap.timestamps)) {
    const keys = new Set(snap.timestamps.map((t) => t.toFixed(3)));
    for (const [key, d] of sliding.dots) {
      if (!keys.has(key)) {
        d.el.remove();
        sliding.dots.delete(key);
      }
    }
    for (const t of snap.timestamps) slidingAddDot(t, true, t.toFixed(3));
  }

  positionSliding();

  const count = Array.isArray(snap.timestamps) ? snap.timestamps.length : sliding.dots.size;
  const caption = container.querySelector('#sw-caption');
  if (caption) {
    caption.textContent =
      'Requests in the window: ' + count + ' / ' + snap.limit +
      ' \u00b7 remaining ' + snap.remaining + ' \u00b7 earliest expires in ' + snap.reset + 's';
  }
}

function animateSliding() {
  positionSliding();
}

/* ---------- TOKEN BUCKET ---------- */

function buildToken(snap) {
  const container = $('#viz-container');
  const maxCoins = Math.min(snap.capacity, 60);
  let coins = '';
  for (let i = 0; i < maxCoins; i++) coins += '<div class="tb-coin"></div>';

  container.innerHTML =
    '<div class="tb-bucket">' +
    '<div class="tb-vessel" id="tb-vessel">' +
    '<div class="tb-capacity-line"></div>' +
    '<div class="tb-fill" id="tb-fill"></div>' +
    '<div class="tb-spark"></div>' +
    '</div>' +
    '<div class="tb-coins">' + coins + '</div>' +
    '<div class="tb-readout">' +
    '<div class="big" id="tb-tokens">0</div>' +
    '<div class="sub" id="tb-cap"></div>' +
    '<div class="sub" id="tb-rate"></div>' +
    '<div class="sub" id="tb-rejected-count"></div>' +
    '</div>' +
    '</div>' +
    '<div class="tb-message" id="tb-message"></div>';

  container.dataset.kind = 'token_bucket';
  container.dataset.fp = snap.capacity + '|' + snap.refill_rate.toFixed(4);
}

function updateTokenLevel(tokens) {
  const container = $('#viz-container');
  const cap = parseInt(container.dataset.fp.split('|')[0], 10) || 1;
  const clamped = Math.min(cap, Math.max(0, tokens));
  const pct = (clamped / cap) * 100;

  const fill = container.querySelector('#tb-fill');
  const readout = container.querySelector('#tb-tokens');
  const coins = container.querySelectorAll('.tb-coin');
  const capEl = container.querySelector('#tb-cap');

  if (fill) {
    fill.style.height = pct + '%';
    fill.classList.toggle('full', clamped >= cap);
  }
  if (readout) readout.textContent = clamped.toFixed(2);
  if (capEl) capEl.textContent = 'tokens / ' + cap + ' capacity';

  const filled = Math.floor(clamped + 1e-6);
  coins.forEach((c, i) => c.classList.toggle('filled', i < filled));
}

function renderToken(snap) {
  const container = $('#viz-container');
  const fp = snap.capacity + '|' + snap.refill_rate.toFixed(4);

  if (container.dataset.kind !== 'token_bucket' || container.dataset.fp !== fp) {
    buildToken(snap);
  }

  updateTokenLevel(snap.tokens != null ? snap.tokens : snap.remaining);

  const rate = container.querySelector('#tb-rate');
  const rej = container.querySelector('#tb-rejected-count');
  if (rate) rate.textContent = 'refill ' + snap.refill_rate.toFixed(3) + ' token/s';
  if (rej) rej.textContent = 'rejected ' + state.metrics.rejected;

  const msg = container.querySelector('#tb-message');
  if (msg) {
    if (state.lastRejected) {
      msg.textContent = '\u2717 No token available \u2014 request rejected';
      msg.className = 'tb-message rejected';
    } else if ((snap.tokens != null ? snap.tokens : snap.remaining) < 1) {
      msg.textContent = 'Empty bucket \u2014 waiting for a token to refill\u2026';
      msg.className = 'tb-message';
    } else {
      msg.textContent = 'Bucket ready \u2014 a request will consume one token.';
      msg.className = 'tb-message';
    }
  }
}

function animateToken() {
  const snap = state.lastSnapshot;
  if (!snap || snap.algorithm !== 'token_bucket') return;

  const elapsed = Math.max(0, serverNow() - state.lastSnapshotNow);
  const tokens = Math.min(snap.capacity, Math.max(0, (snap.tokens != null ? snap.tokens : snap.remaining) + elapsed * snap.refill_rate));
  updateTokenLevel(tokens);
}

/* ---------- LEAKY BUCKET ---------- */

function buildLeaky(snap) {
  const container = $('#viz-container');
  leaky.chips.forEach((c) => c.el.remove());
  leaky.chips.clear();
  leaky.capacity = snap.capacity;
  leaky.shift = 0;

  let slots = '';
  for (let i = 0; i < snap.capacity; i++) slots += '<div class="lb-slot" style="top:' + (8 + i * 40) + 'px"></div>';

  container.innerHTML =
    '<div class="lb-queue">' +
    '<div class="lb-vessel" id="lb-vessel">' +
    '<div class="lb-inlet"></div>' +
    '<div class="lb-inlet-arrow">\u25bc</div>' +
    slots +
    '<div class="lb-drain"></div>' +
    '</div>' +
    '<div class="lb-readout">' +
    '<div class="big" id="lb-queued">0</div>' +
    '<div class="sub" id="lb-cap"></div>' +
    '<div class="sub" id="lb-rate"></div>' +
    '<div class="sub" id="lb-rejected-count"></div>' +
    '</div>' +
    '</div>' +
    '<div class="lb-message" id="lb-message"></div>';

  container.dataset.kind = 'leaky_bucket';
  container.dataset.fp = snap.capacity + '|' + snap.leak_rate.toFixed(4);
  leaky.vessel = container.querySelector('#lb-vessel');
}

function leakyAddChip(key, ts, qi) {
  if (!leaky.vessel) return;

  if (leaky.chips.has(key)) {
    leaky.chips.get(key).qi = qi;
    return;
  }

  const el = document.createElement('div');
  el.className = 'lb-chip';
  el.textContent = '#' + leaky.seq++;
  leaky.vessel.appendChild(el);
  leaky.chips.set(key, { key, ts, qi, el });
}

function slotTop(i) {
  return 8 + i * 40;
}

function positionLeaky() {
  if (!leaky.vessel) return;
  const cap = leaky.capacity;
  const bottom = slotTop(cap - 1);

  for (const chip of leaky.chips.values()) {
    const base = slotTop(cap - 1 - chip.qi);
    chip.el.style.top = Math.min(bottom, base + leaky.shift * 40) + 'px';
  }
}

function renderLeaky(snap) {
  const container = $('#viz-container');
  const fp = snap.capacity + '|' + snap.leak_rate.toFixed(4);

  if (container.dataset.kind !== 'leaky_bucket' || container.dataset.fp !== fp) {
    buildLeaky(snap);
  }

  if (Array.isArray(snap.timestamps)) {
    leaky.shift = 0;
    const keys = new Set(snap.timestamps.map((t) => t.toFixed(3)));
    for (const [key, chip] of leaky.chips) {
      if (!keys.has(key)) {
        chip.el.remove();
        leaky.chips.delete(key);
      }
    }
    snap.timestamps.forEach((t, i) => leakyAddChip(t.toFixed(3), t, i));
  }

  positionLeaky();

  const queued = Array.isArray(snap.timestamps) ? snap.timestamps.length : leaky.chips.size;
  const cap = container.querySelector('#lb-cap');
  const rate = container.querySelector('#lb-rate');
  const rej = container.querySelector('#lb-rejected-count');
  if (cap) cap.textContent = 'queued / ' + snap.capacity + ' capacity';
  if (rate) rate.textContent = 'drain ' + snap.leak_rate.toFixed(3) + ' req/s';
  if (rej) rej.textContent = 'rejected ' + state.metrics.rejected;

  const q = container.querySelector('#lb-queued');
  if (q) q.textContent = queued;

  const msg = container.querySelector('#lb-message');
  if (msg) {
    if (state.lastRejected) {
      msg.textContent = '\u2717 Bucket full \u2014 request rejected (no space)';
      msg.className = 'lb-message rejected';
    } else if (queued > 0) {
      msg.textContent = 'Requests drain FIFO at the constant leak rate.';
      msg.className = 'lb-message';
    } else {
      msg.textContent = 'Bucket empty \u2014 ready for requests.';
      msg.className = 'lb-message';
    }
  }
}

function animateLeaky() {
  const snap = state.lastSnapshot;
  if (!snap || snap.algorithm !== 'leaky_bucket') return;
  if (!leaky.vessel || leaky.chips.size === 0) return;

  const elapsed = Math.max(0, serverNow() - state.lastSnapshotNow);
  leaky.shift = elapsed * (snap.leak_rate || 0);
  positionLeaky();

  if (leaky.shift >= 1) {
    const drained = Math.floor(leaky.shift);
    const ordered = Array.from(leaky.chips.values()).sort((a, b) => a.qi - b.qi);

    for (let i = 0; i < drained && ordered.length; i++) {
      const chip = ordered.shift();
      if (chip) {
        chip.el.classList.add('leaving');
        setTimeout(() => chip.el.remove(), 250);
        leaky.chips.delete(chip.key);
      }
    }

    ordered.forEach((chip, i) => { chip.qi = i; });
    leaky.shift -= drained;
    positionLeaky();
  }
}

/* ---------- animation loop ---------- */

let rafId = null;

function startAnimation() {
  if (rafId) return;

  function loop() {
    animate();
    rafId = requestAnimationFrame(loop);
  }

  rafId = requestAnimationFrame(loop);
}

function animate() {
  if (REDUCED) return;
  const snap = state.lastSnapshot;
  if (!snap) return;

  const algo = activeAlgorithm();
  if (algo !== snap.algorithm) return;

  switch (algo) {
    case 'fixed_window': animateFixed(); break;
    case 'sliding_window': animateSliding(); break;
    case 'token_bucket': animateToken(); break;
    case 'leaky_bucket': animateLeaky(); break;
  }
}

/* ---------- error handling ---------- */

async function handleSimError(res) {
  let msg = 'Request failed (' + res.status + ')';

  try {
    const j = await res.json();
    if (j.detail) msg = typeof j.detail === 'string' ? j.detail : (j.detail.error || JSON.stringify(j.detail));
  } catch {
    /* keep default message */
  }

  toast(msg, false);

  if (res.status === 503) {
    const b = $('#backend-note');
    if (b) b.textContent = 'Redis unavailable \u2014 requests fail (no memory fallback).';
    renderRedisBadge();
  }
}

function triggerReject(el, cls) {
  if (!el) return;
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), 500);
}

/* ---------- mode / traffic ---------- */

function setMode(mode) {
  state.mode = mode;

  $$('#mode-switch .seg').forEach((b) => b.classList.toggle('active', b.dataset.mode === mode));

  const badge = $('#mode-badge');
  badge.textContent = mode === 'simulation' ? 'Simulation' : 'Live API';
  badge.classList.toggle('pill-ok', mode === 'simulation');

  $('#mode-note').textContent =
    mode === 'simulation'
      ? 'Runs the real RateGuard algorithms (memory or Redis). No server config needed.'
      : 'Sends real HTTP requests through the RateGuard API and its ASGI rate-limit middleware. Reset only clears this local view \u2014 the server limiter is untouched.';

  const label = $('#client-id-label');
  const note = $('#client-id-note');
  if (mode === 'live') {
    label.textContent = 'X-API-Key (optional)';
    note.textContent = 'Sent as X-API-Key. Managed keys are authenticated; others act as an opaque client identity. Never stored or persisted.';
  } else {
    label.textContent = 'Client ID';
    note.textContent = 'Identity used to key the rate limit.';
  }

  $$('.live-only').forEach((el) => el.classList.toggle('hidden', mode !== 'live'));
  $('#live-config').classList.toggle('hidden', mode !== 'live');

  const simOnly = $$('#algorithm-select, #backend-select');
  simOnly.forEach((el) => el.classList.toggle('disabled', mode === 'live'));
  $('#limit').disabled = mode === 'live';
  $('#window').disabled = mode === 'live';

  stopAuto();
  resetTallies();
  clearEvents();
  setBackendNote();

  if (mode === 'live') {
    updateVizTitle();
    ingestLiveInitial();
    renderAll();
  } else if (!state.sessionId) {
    createSession();
  } else {
    renderAll();
  }
}

function selectAlgo(algo) {
  state.algorithm = algo;
  $$('#algorithm-select .seg').forEach((b) => b.classList.toggle('active', b.dataset.algo === algo));
  if (state.mode === 'simulation') createSession();
}

function selectBackend(backend) {
  state.backend = backend;
  $$('#backend-select .seg').forEach((b) => b.classList.toggle('active', b.dataset.backend === backend));
  setBackendNote();
  if (state.mode === 'simulation') createSession();
}

function burstCount() {
  readConfig();
  return Math.max(10, Math.min(200, state.limit * 2));
}

async function send(count) {
  if (state.sending) return;
  state.sending = true;
  try {
    if (state.mode === 'simulation') await sendSim(count);
    else await sendLive(count);
  } finally {
    state.sending = false;
  }
}

function startAuto() {
  if (state.autoTimer) return;
  const interval = clampInt($('#auto-speed').value, 50, 5000, 500);
  state.autoTimer = setInterval(() => { send(1); }, interval);
  $('#auto-start').disabled = true;
  $('#auto-stop').disabled = false;
}

function stopAuto() {
  clearInterval(state.autoTimer);
  state.autoTimer = null;
  $('#auto-start').disabled = false;
  $('#auto-stop').disabled = true;
}

async function resetAll() {
  stopAuto();
  resetTallies();

  if (state.mode === 'simulation') {
    await resetSim();
  } else {
    clearEvents();
    state.metrics = Object.assign({}, liveMetrics());
    renderAll();
  }
}

function resetTallies() {
  state.liveRequests = 0;
  state.liveAllowed = 0;
  state.liveRejected = 0;
  state.liveStarted = 0;
  state.lastLive = null;
  sliding.dots.forEach((d) => d.el.remove());
  sliding.dots.clear();
  leaky.chips.forEach((c) => c.el.remove());
  leaky.chips.clear();
}

/* ---------- events ---------- */

function onConfigInput() {
  if (state.mode !== 'simulation') return;
  debouncedCreateSession();
}

const debouncedCreateSession = debounce(createSession, 300);

function bindEvents() {
  $$('#mode-switch .seg').forEach((b) => b.addEventListener('click', () => setMode(b.dataset.mode)));
  $$('#algorithm-select .seg').forEach((b) => b.addEventListener('click', () => selectAlgo(b.dataset.algo)));
  $$('#backend-select .seg').forEach((b) => b.addEventListener('click', () => selectBackend(b.dataset.backend)));

  ['limit', 'window'].forEach((id) => $('#' + id).addEventListener('input', onConfigInput));
  $('#client-id').addEventListener('input', onConfigInput);
  $('#route').addEventListener('change', onConfigInput);

  $('#api-base').addEventListener('input', () => { state.apiBase = $('#api-base').value.trim(); });

  $('#auto-speed').addEventListener('change', () => {
    if (state.autoTimer) {
      stopAuto();
      startAuto();
    }
  });

  $('#send-1').addEventListener('click', () => send(1));
  $('#send-5').addEventListener('click', () => send(5));
  $('#send-burst').addEventListener('click', () => send(burstCount()));
  $('#auto-start').addEventListener('click', startAuto);
  $('#auto-stop').addEventListener('click', stopAuto);
  $('#reset').addEventListener('click', resetAll);
}

document.addEventListener('DOMContentLoaded', init);