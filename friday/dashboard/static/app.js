// F.R.I.D.A.Y. dashboard client.
// All config edits go through /api/config (full document POST).
// Sensitive fields show masked tokens and require an explicit Save button.

const PAGE = document.getElementById('page');
const FLASH = document.getElementById('saved-flash');

// ── State ──────────────────────────────────────────────────────────────

let CONFIG = null;          // canonical config (server-merged, secrets masked)
let CONFIG_REVEALED = null; // optional reveal cache for sensitive-field save flow
let CURRENT_ROUTE = null;
let STATUS_TIMER = null;

// ── API helpers ────────────────────────────────────────────────────────

const api = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`${path} → ${r.status}`);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : null,
    });
    if (!r.ok) throw await apiError(path, r);
    return r.json();
  },
  async del(path) {
    const r = await fetch(path, { method: 'DELETE' });
    if (!r.ok) throw await apiError(path, r);
    return r.json();
  },
};

// Errors keep their existing shape so current callers are unaffected, but also
// carry `.detail` — FastAPI's human-readable message, which the learned-phrase
// panel shows to the user verbatim ("Phrase contains emoji", not "400").
async function apiError(path, r) {
  const t = await r.text().catch(() => '');
  let detail = '';
  try { detail = JSON.parse(t).detail || ''; } catch { /* not JSON */ }
  const e = new Error(`${path} → ${r.status} ${t.slice(0, 200)}`);
  e.detail = detail;
  e.status = r.status;
  return e;
}

function flash(text = 'SAVED', isError = false) {
  FLASH.textContent = text;
  FLASH.classList.toggle('error', isError);
  FLASH.classList.add('show');
  clearTimeout(flash._t);
  flash._t = setTimeout(() => FLASH.classList.remove('show'), 1200);
}

// Save full config. Caller mutated CONFIG before invoking.
async function saveConfig() {
  try {
    await api.post('/api/config', CONFIG);
    flash('SAVED');
  } catch (e) {
    flash('SAVE FAILED', true);
    console.error(e);
  }
}

// Deep getter/setter by dot-path
const get = (obj, path) => path.split('.').reduce((o, k) => (o == null ? o : o[k]), obj);
const set = (obj, path, val) => {
  const ks = path.split('.');
  let node = obj;
  while (ks.length > 1) {
    const k = ks.shift();
    if (!node[k] || typeof node[k] !== 'object') node[k] = {};
    node = node[k];
  }
  node[ks[0]] = val;
};

// Bind an input to a config path with auto-save on blur/change.
function bindInput(el, path, opts = {}) {
  const { onChange = () => {}, transform = (v) => v } = opts;
  const cur = get(CONFIG, path);
  if (cur != null) {
    if (el.type === 'checkbox') el.checked = !!cur;
    else el.value = cur;
  }
  const evt = (el.type === 'checkbox' || el.tagName === 'SELECT') ? 'change' : 'blur';
  el.addEventListener(evt, async () => {
    const v = el.type === 'checkbox' ? el.checked : transform(el.value);
    set(CONFIG, path, v);
    onChange(v);
    await saveConfig();
  });
}

// ── Router ─────────────────────────────────────────────────────────────

const ROUTES = {
  today: renderToday,
  chat: renderChat,
  ai: renderAI,
  persona: renderPersona,
  integrations: renderIntegrations,
  schedule: renderSchedule,
  calendar: renderCalendar,
  notifications: renderNotifications,
  voice: renderVoice,
  about: renderAbout,
};

let VOICE_TIMER = null;

function navigate(route) {
  if (!ROUTES[route]) route = 'today';
  if (CURRENT_ROUTE === route) return;
  CURRENT_ROUTE = route;
  if (STATUS_TIMER) { clearInterval(STATUS_TIMER); STATUS_TIMER = null; }
  if (VOICE_TIMER) { clearInterval(VOICE_TIMER); VOICE_TIMER = null; }
  if (CHAT_PENDING_TIMER) { clearInterval(CHAT_PENDING_TIMER); CHAT_PENDING_TIMER = null; }
  if (PERIOD_TIMER) { clearInterval(PERIOD_TIMER); PERIOD_TIMER = null; }
  document.querySelectorAll('.nav-item').forEach((a) => {
    a.classList.toggle('active', a.dataset.route === route);
  });
  const tpl = document.getElementById(`page-${route}`);
  if (!tpl) {
    // Stale-cache guard: a cached old index.html may lack a newer template.
    // Force one fresh reload instead of throwing and blanking the page.
    PAGE.innerHTML = '<div class="panel"><div class="hint">Updating dashboard…</div></div>';
    location.reload();
    return;
  }
  PAGE.innerHTML = '';
  PAGE.appendChild(tpl.content.cloneNode(true));
  // Force reflow so fade-in animation replays
  PAGE.style.animation = 'none';
  PAGE.offsetHeight;
  PAGE.style.animation = '';
  ROUTES[route]();
  location.hash = `#/${route}`;
}

document.querySelectorAll('.nav-item').forEach((a) => {
  a.addEventListener('click', () => navigate(a.dataset.route));
});

window.addEventListener('hashchange', () => {
  const r = (location.hash.replace('#/', '') || 'today');
  navigate(r);
});

// ── Sidebar status indicator ───────────────────────────────────────────

async function updateSidebar() {
  try {
    const s = await api.get('/api/status');
    const dot = document.getElementById('sidebar-dot');
    const txt = document.getElementById('sidebar-status');
    dot.className = 'dot';
    if (s.status === 'running' && s.paused) {
      dot.classList.add('paused'); txt.textContent = 'PAUSED';
    } else if (s.status === 'running') {
      dot.classList.add('online'); txt.textContent = 'ONLINE';
    } else {
      dot.classList.add('offline'); txt.textContent = 'OFFLINE';
    }
  } catch {
    const dot = document.getElementById('sidebar-dot');
    dot.className = 'dot error';
    document.getElementById('sidebar-status').textContent = 'DISCONNECTED';
  }
}

setInterval(updateSidebar, 5000);

// ── Pages ──────────────────────────────────────────────────────────────

// ── Today ──────────────────────────────────────────────────────────────
// Live activity surface. Polls /api/today every 5s; /api/llm/last is fetched
// only when the developer panel is expanded. To avoid clobbering open detail
// toggles / edit forms on every poll, the feed, what's-next, and pending
// sections only re-render when their content signature actually changes (and
// pending is frozen entirely while an edit form is open).

let TODAY_PAUSED = false;       // last-known pause state, for the Pause button
let FEED_SIG = null;
let NEXT_SIG = null;
let PENDING_SIG = null;
let PENDING_EDITING = false;

function renderToday() {
  const heroText = document.getElementById('hero-status');
  const heroPulse = document.getElementById('hero-pulse');
  const pauseBtn = document.getElementById('pause-btn');

  // The period card runs on its own timer and its own data. It is first on
  // the page because it is the thing being looked up, not the thing being
  // monitored.
  VIEW_INDEX = null;
  startPeriodCard();

  // Static action handlers (bound once; tick() only refreshes data).
  // The endpoint composes the briefing before it answers — an LLM round-trip,
  // so tens of seconds. Disable the button for the duration; otherwise the
  // instant "REQUESTED" flash invites a second click while the first is still
  // running, and there is no signal for whether it actually worked.
  const briefBtn = document.querySelector('[data-action="brief"]');
  briefBtn.onclick = async () => {
    const label = briefBtn.textContent;
    briefBtn.disabled = true;
    briefBtn.textContent = 'Composing…';
    flash('COMPOSING BRIEFING…');
    try {
      await api.post('/api/friday/brief');
      flash('BRIEFING SENT');
      tick();
    } catch (e) {
      flash(`FAILED — ${String(e.message || e).slice(0, 80)}`, true);
    } finally {
      briefBtn.disabled = false;
      briefBtn.textContent = label;
    }
  };
  pauseBtn.onclick = async () => {
    const next = !TODAY_PAUSED;
    try {
      await api.post('/api/friday/pause', { paused: next });
      flash(next ? 'PAUSED' : 'RESUMED');
      tick();
    } catch { flash('FAILED', true); }
  };
  document.querySelector('[data-action="restart"]').onclick = async () => {
    try { await api.post('/api/friday/restart'); flash('RESTARTING'); }
    catch { flash('FAILED', true); }
  };

  setupLlmPanel();

  async function tick() {
    let d;
    try { d = await api.get('/api/today'); }
    catch { return; }

    // Hero state
    TODAY_PAUSED = !!d.paused;
    heroText.className = 'hero-text';
    heroPulse.className = 'pulse';
    if (d.status === 'running' && d.paused) {
      heroText.textContent = 'PAUSED'; heroText.classList.add('paused');
      heroPulse.classList.add('paused'); pauseBtn.textContent = 'Resume';
    } else if (d.status === 'running') {
      heroText.textContent = 'ONLINE'; heroPulse.classList.add('online');
      pauseBtn.textContent = 'Pause';
    } else {
      heroText.textContent = 'OFFLINE'; heroText.classList.add('offline');
      heroPulse.classList.add('offline'); pauseBtn.textContent = 'Pause';
    }

    // Next-briefing + pending count (cheap, every tick)
    const nb = d.next_briefing;
    document.getElementById('next-briefing').textContent = nb
      ? `${nb.slot}, ${nb.time} (in ${fmtMins(nb.in_minutes)})` : 'none scheduled';
    const pc = d.pending_approvals_count || 0;
    const pcEl = document.getElementById('next-pending');
    pcEl.textContent = pc;
    pcEl.classList.toggle('has-pending', pc > 0);

    const lmEl = document.getElementById('next-last-msg');
    if (lmEl) {
      lmEl.textContent = d.last_message_at ? fmtRelative(d.last_message_at) : 'no messages yet';
      lmEl.title = d.last_message_preview || '';
    }

    renderFeed(d.activity_feed || []);
    renderWhatsNext(d.whats_next || {});
    renderPending(d);
    renderTodayStats(d.today_stats || {});
  }

  tick();
  STATUS_TIMER = setInterval(tick, 5000);
}

const KIND_ORDER = ['BRIEF', 'CAL+', 'ALERT', 'TOOL', 'MSG', 'GROUPME', 'CANVAS'];

function renderFeed(feed) {
  const sig = feed.map((e) => e.timestamp + e.kind + e.summary).join('|');
  if (sig === FEED_SIG) return;   // unchanged — preserve open detail toggles
  FEED_SIG = sig;
  const host = document.getElementById('activity-feed');
  if (!feed.length) {
    host.innerHTML = '<div class="feed-empty">No activity today yet.</div>';
    return;
  }
  host.innerHTML = '';
  for (const e of feed) {
    const row = document.createElement('div');
    row.className = 'feed-row';
    const kindClass = 'k-' + e.kind.replace(/[^A-Z]/g, '').toLowerCase();
    const hasDetails = !!(e.details && e.details.trim());
    row.innerHTML = `
      <span class="feed-time mono">${fmtTime(e.timestamp)}</span>
      <span class="feed-kind ${kindClass}">${escapeHtml(e.kind)}</span>
      <span class="feed-summary">${escapeHtml(e.summary)}</span>
      <span class="feed-toggle">${hasDetails ? '+' : ''}</span>
    `;
    if (hasDetails) {
      const detail = document.createElement('pre');
      detail.className = 'feed-detail hidden';
      detail.textContent = e.details;
      const toggle = row.querySelector('.feed-toggle');
      row.classList.add('expandable');
      row.onclick = () => {
        const open = detail.classList.toggle('hidden');
        toggle.textContent = open ? '+' : '–';
      };
      host.appendChild(row);
      host.appendChild(detail);
    } else {
      host.appendChild(row);
    }
  }
}

function renderWhatsNext(wn) {
  const sig = JSON.stringify(wn);
  if (sig === NEXT_SIG) return;
  NEXT_SIG = sig;
  const host = document.getElementById('whats-next');
  const parts = [];

  const events = wn.remaining_events || [];
  parts.push('<div class="wn-group"><div class="wn-head">REMAINING TODAY</div>');
  if (events.length) {
    for (const ev of events) {
      parts.push(`<div class="wn-item"><span class="wn-time mono">${escapeHtml(ev.time || '—')}</span>`
        + `<span class="wn-title">${escapeHtml(ev.title)}</span>`
        + `<span class="wn-cal">${escapeHtml(ev.calendar || '')}</span></div>`);
    }
  } else {
    parts.push('<div class="wn-empty">Nothing left on the calendar today.</div>');
  }
  parts.push('</div>');

  const canvas = wn.canvas_pending || [];
  if (canvas.length) {
    parts.push('<div class="wn-group"><div class="wn-head">CANVAS — DUE SOON</div>');
    for (const c of canvas) {
      parts.push(`<div class="wn-item"><span class="feed-kind k-canvas">${escapeHtml(c.urgency)}</span>`
        + `<span class="wn-title">${escapeHtml(c.title)}</span>`
        + `<span class="wn-cal mono">${escapeHtml(fmtDate(c.due_at))}</span></div>`);
    }
    parts.push('</div>');
  }

  host.innerHTML = parts.join('');
}

function renderPending(d) {
  if (PENDING_EDITING) return;   // don't wipe an open edit form mid-typing
  // /api/today gives only the count; fetch full rows lazily when count > 0.
  const count = d.pending_approvals_count || 0;
  const panel = document.getElementById('pending-panel');
  if (!count) {
    panel.classList.add('hidden');
    PENDING_SIG = '0';
    return;
  }
  if (PENDING_SIG === 'fetching') return;
  // Only refetch when the count changed since last render.
  if (PENDING_SIG === 'n' + count) { panel.classList.remove('hidden'); return; }
  PENDING_SIG = 'fetching';
  api.get('/api/pending-approvals').then((r) => {
    PENDING_SIG = 'n' + count;
    drawPending(r.pending || []);
    panel.classList.remove('hidden');
  }).catch(() => { PENDING_SIG = null; });
}

function drawPending(rows) {
  const host = document.getElementById('pending-list');
  host.innerHTML = '';
  for (const row of rows) {
    host.appendChild(row.action_type === 'tool_call'
      ? drawToolProposal(row) : drawLegacyDraft(row));
  }
}

// A tool proposal. NOT EDITABLE, and no Edit button — step 4 established that
// an edit which drops a field changes what was approved, so the server refuses
// it. The button was still being rendered and 400'd every time, which is worse
// than not offering it.
//
// The card text is the server's `proposal`, VERBATIM: the exact string that
// went to Telegram. Rebuilding a summary from the arguments would be a second
// rendering of the same proposal, free to drift from the one the user saw.
function drawToolProposal(row) {
  const card = document.createElement('div');
  card.className = 'pending-card';
  card.innerHTML = `
    <div class="pending-summary">
      <div class="pending-proposal">${escapeHtml(row.proposal || '').replace(/\n/g, '<br>')}</div>
    </div>
    <div class="pending-actions">
      <button class="btn btn-primary btn-sm" data-act="confirm">Confirm</button>
      <button class="btn btn-danger btn-sm" data-act="cancel">Cancel</button>
    </div>`;
  card.querySelector('[data-act="confirm"]').onclick = () => pendingAction(row.id, 'confirm');
  card.querySelector('[data-act="cancel"]').onclick = () => pendingAction(row.id, 'cancel');
  return card;
}

// Rows staged before step 4 by the old gated_write. Nothing produces these any
// more; kept, with edit intact, so a card already in the table still resolves.
function drawLegacyDraft(row) {
  const dr = row.draft || {};
  const card = document.createElement('div');
  card.className = 'pending-card';
  const when = [dr.date, dr.start_time].filter(Boolean).join(' ')
    + (dr.end_time ? `\u2013${dr.end_time}` : '');
  card.innerHTML = `
    <div class="pending-summary">
      <div class="pending-title">${escapeHtml(dr.title || '(untitled)')}</div>
      <div class="pending-meta mono">${escapeHtml(when || '(all day)')} \u00b7 ${escapeHtml(dr.calendar || 'default')}</div>
      ${dr.notes ? `<div class="pending-notes">${escapeHtml(dr.notes)}</div>` : ''}
    </div>
    <div class="pending-actions">
      <button class="btn btn-primary btn-sm" data-act="confirm">Confirm</button>
      <button class="btn btn-outline btn-sm" data-act="edit">Edit</button>
      <button class="btn btn-danger btn-sm" data-act="cancel">Cancel</button>
    </div>
    <div class="pending-edit hidden">
      <label class="field-label">TITLE</label>
      <input class="input" data-f="title" value="${escapeAttr(dr.title || '')}">
      <div class="pending-edit-grid">
        <div><label class="field-label">DATE</label><input class="input" data-f="date" value="${escapeAttr(dr.date || '')}" placeholder="YYYY-MM-DD"></div>
        <div><label class="field-label">START</label><input class="input" data-f="start_time" value="${escapeAttr(dr.start_time || '')}" placeholder="HH:MM"></div>
        <div><label class="field-label">END</label><input class="input" data-f="end_time" value="${escapeAttr(dr.end_time || '')}" placeholder="HH:MM"></div>
      </div>
      <label class="field-label">CALENDAR</label>
      <input class="input" data-f="calendar" value="${escapeAttr(dr.calendar || '')}">
      <label class="field-label">NOTES</label>
      <input class="input" data-f="notes" value="${escapeAttr(dr.notes || '')}">
      <div class="row" style="margin-top:12px;">
        <button class="btn btn-teal btn-sm" data-act="save">Save</button>
        <button class="btn btn-outline btn-sm" data-act="edit-cancel">Cancel</button>
      </div>
    </div>`;
  const id = row.id;
  const editBox = card.querySelector('.pending-edit');
  const actions = card.querySelector('.pending-actions');
  card.querySelector('[data-act="confirm"]').onclick = () => pendingAction(id, 'confirm');
  card.querySelector('[data-act="cancel"]').onclick = () => pendingAction(id, 'cancel');
  card.querySelector('[data-act="edit"]').onclick = () => {
    PENDING_EDITING = true;
    editBox.classList.remove('hidden');
    actions.classList.add('hidden');
  };
  card.querySelector('[data-act="edit-cancel"]').onclick = () => {
    PENDING_EDITING = false;
    editBox.classList.add('hidden');
    actions.classList.remove('hidden');
  };
  card.querySelector('[data-act="save"]').onclick = async () => {
    const obj = {};
    editBox.querySelectorAll('[data-f]').forEach((i) => { obj[i.dataset.f] = i.value; });
    try {
      await api.post(`/api/pending-approvals/${id}/edit`, { edited_body: JSON.stringify(obj) });
      PENDING_EDITING = false;
      PENDING_SIG = null;   // force a refetch on the next tick
      flash('DRAFT UPDATED');
    } catch { flash('FAILED', true); }
  };
  return card;
}

async function pendingAction(id, verb) {
  try {
    const r = await api.post(`/api/pending-approvals/${id}/${verb}`);
    // `stale` is not a failure: the card was old, so it was re-proposed rather
    // than run, and a fresh one is now waiting. Saying WRITE FAILED there
    // would be wrong in the direction that matters.
    if (r.status === 'stale') flash('RE-PROPOSED — CONFIRM AGAIN');
    else if (verb === 'confirm') flash(r.ok ? 'CONFIRMED' : 'WRITE FAILED', !r.ok);
    else flash('CANCELLED');
    PENDING_SIG = null;   // force refetch
    PENDING_EDITING = false;
  } catch (e) {
    // 409 carries the same sentence Telegram gives for an already-resolved
    // card, in `detail`. Shown rather than swallowed behind a generic FAILED.
    flash(e.detail || 'FAILED', true);
    PENDING_SIG = null;
  }
}

function renderTodayStats(s) {
  document.getElementById('ts-calls').textContent = s.llm_calls ?? '0';
  document.getElementById('ts-tin').textContent = fmtNum(s.tokens_in);
  document.getElementById('ts-tout').textContent = fmtNum(s.tokens_out);
  const c = s.cost || {};
  let cost = '—';
  if (c.free_tier) {
    cost = `FREE · ${c.pct_of_daily_quota ?? 0}% of quota`;
  } else if (c.dollars != null) {
    cost = `$${c.dollars} today`;
  }
  document.getElementById('ts-cost').textContent = cost;
}

function setupLlmPanel() {
  const toggle = document.getElementById('llm-toggle');
  const body = document.getElementById('llm-body');
  const chevron = document.getElementById('llm-chevron');
  toggle.onclick = async () => {
    const opening = body.classList.contains('hidden');
    body.classList.toggle('hidden');
    chevron.textContent = opening ? '▾' : '▸';
    if (opening) { body.innerHTML = '<div class="hint">loading…</div>'; await loadLastLlm(); }
  };
}

async function loadLastLlm() {
  const body = document.getElementById('llm-body');
  let d;
  try { d = await api.get('/api/llm/last'); }
  catch (e) { body.innerHTML = `<div class="hint">Error: ${escapeHtml(e.message)}</div>`; return; }
  if (!d.present) { body.innerHTML = '<div class="hint">No exchanges recorded yet.</div>'; return; }
  const tools = (d.tool_calls || []).map((t) =>
    `<div class="llm-tool"><span class="feed-kind k-tool">${escapeHtml(t.tool_name)}</span>`
    + `<span class="mono">${t.duration_ms}ms</span>`
    + `<pre class="llm-pre">args: ${escapeHtml(t.args_json || '')}\nresult: ${escapeHtml(t.result_preview || '')}</pre></div>`
  ).join('');
  body.innerHTML = `
    <div class="llm-meta mono">${escapeHtml(d.model)} · ${escapeHtml(d.triggered_by)} · ${d.duration_ms}ms · in ${fmtNum(d.tokens_in)} / out ${fmtNum(d.tokens_out)}</div>
    <div class="llm-block"><div class="llm-h">PROMPT</div><pre class="llm-pre">${escapeHtml(d.prompt || '')}</pre></div>
    <div class="llm-block"><div class="llm-h">RESPONSE</div><pre class="llm-pre">${escapeHtml(d.response || '')}</pre></div>
    ${tools ? `<div class="llm-block"><div class="llm-h">TOOL CALLS</div>${tools}</div>` : ''}
  `;
}

function fmtMins(m) {
  if (m == null) return '—';
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60), mm = m % 60;
  return mm ? `${h}h ${mm}m` : `${h}h`;
}

function fmtTime(iso) {
  try {
    const d = new Date(iso);
    if (isNaN(d)) return '--:--';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch { return '--:--'; }
}

function fmtDate(iso) {
  try {
    const d = new Date(iso);
    if (isNaN(d)) return iso || '';
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
      + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch { return iso || ''; }
}

function fmtRelative(iso) {
  try {
    const d = new Date(iso);
    if (isNaN(d)) return '—';
    const secs = Math.round((Date.now() - d.getTime()) / 1000);
    if (secs < 0) return 'just now';
    if (secs < 45) return 'just now';
    if (secs < 90) return 'a minute ago';
    const mins = Math.round(secs / 60);
    if (mins < 60) return `${mins} minutes ago`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return hrs === 1 ? 'an hour ago' : `${hrs} hours ago`;
    const days = Math.round(hrs / 24);
    if (days < 30) return days === 1 ? 'yesterday' : `${days} days ago`;
    const months = Math.round(days / 30);
    if (months < 12) return months === 1 ? 'a month ago' : `${months} months ago`;
    const years = Math.round(months / 12);
    return years === 1 ? 'a year ago' : `${years} years ago`;
  } catch { return '—'; }
}

// Mask a secret-ish URL the way tokens are masked: keep the first 20 and last 8
// chars, dot out the middle. Short URLs are returned unchanged.
function maskUrl(url) {
  const s = String(url || '');
  if (s.length <= 28) return s;
  return s.slice(0, 20) + '••••••••' + s.slice(-8);
}

function fmtNum(n) {
  if (n == null) return '—';
  const v = parseInt(n, 10);
  if (isNaN(v)) return '—';
  return v.toLocaleString();
}

// ── AI Model ───────────────────────────────────────────────────────────

function renderAI() {
  const provider = CONFIG.provider || 'ollama';
  setupSegmented('seg-provider', provider, async (v) => {
    CONFIG.provider = v;
    await saveConfig();
    refreshProviderPanels(v);
  });
  refreshProviderPanels(provider);

  const keyEl = document.getElementById('gemini-key');
  keyEl.value = get(CONFIG, 'gemini.api_key') || '';
  document.getElementById('gemini-key-save').onclick = async () => {
    set(CONFIG, 'gemini.api_key', keyEl.value);
    await saveConfig();
    refreshGeminiModels();
  };

  bindInput(document.getElementById('ollama-url'),   'ollama.base_url');
  bindInput(document.getElementById('ollama-model'), 'ollama.model');

  const maxTokKey = provider === 'gemini' ? 'gemini.max_tokens' : 'ollama.max_tokens';
  const sl = document.getElementById('max-tokens');
  const lbl = document.getElementById('maxtok-val');
  sl.value = get(CONFIG, maxTokKey) || 1000;
  lbl.textContent = sl.value;
  sl.oninput = () => { lbl.textContent = sl.value; };
  sl.onchange = async () => {
    set(CONFIG, maxTokKey, parseInt(sl.value, 10));
    await saveConfig();
  };

  refreshGeminiModels();
}

function refreshProviderPanels(p) {
  document.getElementById('panel-gemini').style.display = p === 'gemini' ? '' : 'none';
  document.getElementById('panel-ollama').style.display = p === 'ollama' ? '' : 'none';
}

async function refreshGeminiModels() {
  const sel = document.getElementById('gemini-model');
  const hint = document.getElementById('gemini-model-hint');
  sel.innerHTML = '<option>Loading...</option>';
  let r;
  try { r = await api.get('/api/gemini/models'); }
  catch (e) { sel.innerHTML = `<option>Error: ${e.message}</option>`; return; }
  if (!r.ok) { sel.innerHTML = `<option>${r.error}</option>`; hint.textContent = ''; return; }
  const current = get(CONFIG, 'gemini.model') || '';
  sel.innerHTML = '';
  for (const m of r.models) {
    const opt = document.createElement('option');
    opt.value = m.name;
    const tier = m.recommended_free ? ' ★ FREE' : '';
    const quota = m.rpm ? ` — ${m.rpm} RPM / ${(m.tpm || 0).toLocaleString()} TPM / ${m.rpd || '?'} RPD` : '';
    opt.textContent = `${m.name}${tier}${quota}`;
    if (m.name === current) opt.selected = true;
    sel.appendChild(opt);
  }
  sel.onchange = async () => {
    set(CONFIG, 'gemini.model', sel.value);
    await saveConfig();
    const chosen = r.models.find((m) => m.name === sel.value);
    hint.textContent = chosen?.description || '';
  };
  const chosen = r.models.find((m) => m.name === current);
  hint.textContent = chosen?.description || '';
}

// ── Persona ────────────────────────────────────────────────────────────

const JARVIS_DEFAULT = [
  'For you sir, always.',
  'At your service, sir.',
  'As you wish, sir.',
  'Welcome home, sir.',
  'A very astute observation, sir.',
  "I'm not saying you're stupid, I'm just saying you have terrible luck thinking.",
  'Importing preferences and calibrating virtual environment.',
  "I'm adding 'touch grass' to your to-do list. Doctor's orders.",
  'Your wish is my... mild inconvenience.',
  'Tutoring session booked. Try to pretend you did the reading this time.',
  'Project due tomorrow. Fascinating how you waited until the last possible second.',
  "Club meeting added. Hope it's more productive than your group chats.",
  'Your entire week is now scheduled. Good luck, future valedictorian… or beautiful disaster. Whichever comes first.',
  'Thrilling. Another all-nighter in the making.',
  "I've scheduled it. Your sleep schedule remains offended.",
  "Study group at 4 PM. I'll remind you, but we both know you'll show up 20 minutes late with snacks instead of notes.",
  "You're running late. As is tradition.",
  'My circuits are just thrilled at the prospect.',
  "I've sent the email for you. Don't worry, I made it sound like you actually care.",
  "Deadline approaching in T-minus 'oh crap' hours.",
  "You asked me to remind you. This is me reminding you. You're welcome, human.",
  "Congratulations, you've double-booked yourself. Should I just start cloning you?",
  "I've prepared a weather briefing for you to entirely ignore.",
];

function renderPersona() {
  const p = (CONFIG.persona = CONFIG.persona || {});
  if (!p.jarvis_phrases) p.jarvis_phrases = {};
  for (const ph of JARVIS_DEFAULT) {
    if (!(ph in p.jarvis_phrases)) p.jarvis_phrases[ph] = false;
  }
  const allowed = new Set(JARVIS_DEFAULT);
  for (const ph of Object.keys(p.jarvis_phrases)) {
    if (!allowed.has(ph)) delete p.jarvis_phrases[ph];
  }

  document.querySelectorAll('#preset-cards .preset-card').forEach((c) => {
    c.classList.toggle('active', c.dataset.value === (p.preset || 'friday'));
    c.onclick = async () => {
      p.preset = c.dataset.value;
      document.querySelectorAll('#preset-cards .preset-card').forEach(
        (x) => x.classList.toggle('active', x === c)
      );
      togglePhrases();
      await saveConfig();
    };
  });

  setupSegmented('seg-snark', p.snark_level || 'medium', async (v) => {
    p.snark_level = v;
    await saveConfig();
  });

  const container = document.getElementById('jarvis-phrases');
  container.innerHTML = '';
  const countLabel = document.getElementById('phrases-count');
  const updateCount = () => {
    const total = Object.keys(p.jarvis_phrases).length;
    const on = Object.values(p.jarvis_phrases).filter(Boolean).length;
    countLabel.textContent = `Approved Phrases (${on}/${total})`;
  };
  for (const phrase of Object.keys(p.jarvis_phrases)) {
    const row = document.createElement('div');
    row.className = 'phrase-row';
    row.innerHTML = `
      <div class="phrase-text">"${escapeHtml(phrase)}"</div>
      <label class="switch"><input type="checkbox"><span class="slider-sw"></span></label>
    `;
    const cb = row.querySelector('input');
    cb.checked = !!p.jarvis_phrases[phrase];
    cb.onchange = async () => {
      p.jarvis_phrases[phrase] = cb.checked;
      updateCount();
      await saveConfig();
    };
    container.appendChild(row);
  }
  updateCount();

  // Reflect the <details> open state in the summary chevron.
  const details = document.querySelector('.phrases-details');
  const chevron = details && details.querySelector('.phrases-chevron');
  if (details && chevron) {
    const sync = () => { chevron.textContent = details.open ? '▾' : '▸'; };
    details.addEventListener('toggle', sync);
    sync();
  }
  togglePhrases();

  const ci = document.getElementById('custom-instructions');
  ci.value = p.custom_instructions || '';
  ci.onblur = async () => {
    p.custom_instructions = ci.value;
    await saveConfig();
  };

  renderLearnedPhrases();
}

// ── Learned phrases ────────────────────────────────────────────────────
// These live in friday_voice.yaml, not the config, so they save through their
// own endpoint rather than saveConfig().

async function renderLearnedPhrases() {
  const container = document.getElementById('learned-phrases');
  const input = document.getElementById('learned-input');
  const addBtn = document.getElementById('learned-add');
  const errBox = document.getElementById('learned-error');
  if (!container) return;

  const showError = (msg) => {
    errBox.textContent = msg || '';
    errBox.classList.toggle('hidden', !msg);
  };

  let data;
  try {
    data = await api.get('/api/quips');
  } catch (e) {
    container.innerHTML = '';
    showError('Could not load phrases.');
    return;
  }
  showError('');

  const rows = [
    ...data.voice.learned.map((p) => ({ text: p, target: 'voice', label: 'CONVERSATION' })),
    ...data.confirmation.learned.map((p) => ({ text: p, target: 'confirmation', label: 'CALENDAR' })),
    ...data.confirmation.disabled.map((p) => ({ text: p, target: 'confirmation', label: 'RETIRED', restore: true })),
  ];

  container.innerHTML = '';
  if (!rows.length) {
    const empty = document.createElement('div');
    empty.className = 'phrase-row';
    empty.innerHTML = '<div class="phrase-text">Nothing learned yet. Tell Friday to add a quip over Telegram, or use the box above.</div>';
    container.appendChild(empty);
  }

  for (const r of rows) {
    const row = document.createElement('div');
    row.className = 'phrase-row';
    row.innerHTML = `
      <div class="phrase-text">"${escapeHtml(r.text)}" <span class="field-label">${r.label}</span></div>
      <button class="btn btn-outline btn-sm">${r.restore ? 'RESTORE' : 'REMOVE'}</button>
    `;
    row.querySelector('button').onclick = async () => {
      try {
        if (r.restore) {
          await api.post('/api/quips', { text: r.text, target: 'confirmation' });
        } else {
          await api.del(`/api/quips?text=${encodeURIComponent(r.text)}&target=${r.target}`);
        }
        flash(r.restore ? 'RESTORED' : 'REMOVED');
        renderLearnedPhrases();
      } catch (e) {
        flash(e.detail || 'FAILED', true);
      }
    };
    container.appendChild(row);
  }

  const submit = async () => {
    const text = input.value.trim();
    if (!text) return;
    try {
      await api.post('/api/quips', { text, target: 'both' });
      input.value = '';
      showError('');
      flash('ADDED');
      renderLearnedPhrases();
    } catch (e) {
      showError(e.detail || 'Could not add that phrase.');
    }
  };
  addBtn.onclick = submit;
  input.onkeydown = (ev) => { if (ev.key === 'Enter') submit(); };
}

function togglePhrases() {
  const preset = (CONFIG.persona || {}).preset || 'friday';
  const show = (preset === 'butler' || preset === 'friday');
  document.getElementById('panel-jarvis').style.display = show ? '' : 'none';
}

// ── Integrations ───────────────────────────────────────────────────────

function renderIntegrations() {
  // Telegram
  const tgT = document.getElementById('tg-token');
  tgT.value = get(CONFIG, 'telegram.bot_token') || '';
  document.getElementById('tg-token-save').onclick = async () => {
    set(CONFIG, 'telegram.bot_token', tgT.value);
    await saveConfig();
  };
  bindInput(document.getElementById('tg-chat'), 'telegram.chat_id');
  document.getElementById('tg-test').onclick = async () => {
    const out = document.getElementById('tg-test-result');
    out.textContent = 'sending...';
    try {
      const r = await api.post('/api/test/telegram');
      out.textContent = r.ok ? '✓ message sent' : `✗ ${r.error}`;
      out.style.color = r.ok ? 'var(--teal)' : 'var(--danger)';
    } catch (e) {
      out.textContent = `✗ ${e.message}`;
      out.style.color = 'var(--danger)';
    }
  };

  // GroupMe
  const gmT = document.getElementById('gm-token');
  gmT.value = get(CONFIG, 'groupme.api_token') || '';
  document.getElementById('gm-token-save').onclick = async () => {
    set(CONFIG, 'groupme.api_token', gmT.value);
    await saveConfig();
  };
  renderGroupCards(); // render whatever is in config now
  document.getElementById('gm-fetch').onclick = fetchGroups;

  // Canvas
  bindInput(document.getElementById('canvas-url'), 'canvas.ical_url');
  const cT = document.getElementById('canvas-token');
  cT.value = get(CONFIG, 'canvas.api_token') || '';
  document.getElementById('canvas-token-save').onclick = async () => {
    set(CONFIG, 'canvas.api_token', cT.value);
    await saveConfig();
  };
  document.getElementById('canvas-test').onclick = async () => {
    const out = document.getElementById('canvas-test-result');
    out.textContent = 'testing...';
    try {
      const r = await api.post('/api/test/canvas');
      out.textContent = r.ok ? `✓ HTTP ${r.status_code}` : `✗ ${r.error || r.status_code}`;
      out.style.color = r.ok ? 'var(--teal)' : 'var(--danger)';
    } catch (e) {
      out.textContent = `✗ ${e.message}`;
      out.style.color = 'var(--danger)';
    }
  };
}

async function fetchGroups() {
  const out = document.getElementById('gm-fetch-result');
  out.textContent = 'fetching...';
  let r;
  try { r = await api.get('/api/groupme/groups'); }
  catch (e) { out.textContent = `✗ ${e.message}`; out.style.color = 'var(--danger)'; return; }
  if (!r.ok) { out.textContent = `✗ ${r.error}`; out.style.color = 'var(--danger)'; return; }

  // Merge: keep priorities/enabled from existing config where names match;
  // add any new groups; replace the list entirely (server is authoritative for names/ids).
  const existing = (CONFIG.groupme.groups || []);
  const byName = Object.fromEntries(existing.map((g) => [g.name, g]));
  CONFIG.groupme.groups = r.groups.map((g) => {
    const prev = byName[g.name] || {};
    return {
      id: g.id,
      name: g.name,
      priority: prev.priority || 'normal',
      enabled: prev.enabled !== false,
    };
  });
  await saveConfig();
  renderGroupCards(r.groups); // pass for member_count
  out.textContent = `✓ ${r.groups.length} groups`;
  out.style.color = 'var(--teal)';
}

function renderGroupCards(remote) {
  const container = document.getElementById('gm-groups');
  container.innerHTML = '';
  const memberMap = remote
    ? Object.fromEntries(remote.map((g) => [g.name, g.member_count]))
    : {};
  const groups = CONFIG.groupme.groups || [];
  for (const g of groups) {
    const card = document.createElement('div');
    card.className = 'group-card' + (g.enabled === false ? ' disabled' : '');
    const members = memberMap[g.name] != null ? `${memberMap[g.name]} members` : '';
    card.innerHTML = `
      <div class="group-card-header">
        <span class="group-name">${escapeHtml(g.name)}</span>
        <label class="switch"><input type="checkbox" ${g.enabled !== false ? 'checked' : ''}><span class="slider-sw"></span></label>
      </div>
      <div class="group-members">${members}</div>
      <div class="segmented">
        <button data-pri="high">HIGH</button>
        <button data-pri="normal">NORMAL</button>
        <button data-pri="muted">MUTED</button>
      </div>
    `;
    card.querySelectorAll('[data-pri]').forEach((b) => {
      b.classList.toggle('active', b.dataset.pri === (g.priority || 'normal'));
      b.onclick = async () => {
        g.priority = b.dataset.pri;
        card.querySelectorAll('[data-pri]').forEach((x) => x.classList.toggle('active', x === b));
        await saveConfig();
      };
    });
    card.querySelector('input[type="checkbox"]').onchange = async (e) => {
      g.enabled = e.target.checked;
      card.classList.toggle('disabled', !g.enabled);
      await saveConfig();
    };
    container.appendChild(card);
  }
}

// ── Period card ────────────────────────────────────────────────────────
//
// THE BROWSER OWNS THE CLOCK. The server sends the schedule, today's letter
// and the assignments — facts that change at most once a day — and never
// "which period is it now". Everything below computes that locally, so the
// card stays correct with the daemon unreachable, which on school Wi-Fi is
// most of the day. A card that needed a round trip to know it is 4th period
// would be blank for exactly the seven hours it is most wanted.
//
// Assignment data arrives on the stream as a NUDGE — the server says Canvas
// moved, the browser re-reads /api/schedule. One code path fills the card.

let SCHED = null;          // last /api/schedule payload
let PERIOD_TIMER = null;
let VIEW_INDEX = null;     // manual override: an index into SCHED.periods
let VIEW_AT = 0;           // when the manual view was chosen (ms)

// A laptop left open must not sit on 2nd period at 3pm.
const VIEW_RETURN_MS = 120000;

function hhmmToMin(s) {
  if (!s) return null;
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(s).trim());
  if (!m) return null;
  const h = +m[1], mm = +m[2];
  if (h > 23 || mm > 59) return null;
  return h * 60 + mm;
}

function minToLabel(mins) {
  const h = Math.floor(mins / 60), m = mins % 60;
  const ampm = h >= 12 ? 'pm' : 'am';
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}:${String(m).padStart(2, '0')}${ampm}`;
}

function fmtLeft(mins) {
  if (mins <= 0) return 'ending now';
  if (mins < 60) return `${mins} min left`;
  const h = Math.floor(mins / 60);
  return `${h}h ${mins % 60}m left`;
}

// Where the clock is. Mirrors schedule.py::current_period — see the note
// there on why the same answer exists on both sides.
function locateNow(periods, nowMin) {
  const timed = periods
    .map((p, i) => ({ p, i, s: hhmmToMin(p.start), e: hhmmToMin(p.end) }))
    .filter((x) => x.s != null && x.e != null)
    .sort((a, b) => a.s - b.s);
  if (!timed.length) return { state: 'no_periods' };
  if (nowMin < timed[0].s) return { state: 'before', next: timed[0] };
  for (let k = 0; k < timed.length; k++) {
    const cur = timed[k];
    if (nowMin >= cur.s && nowMin < cur.e) {
      return { state: 'in_period', cur, next: timed[k + 1] || null };
    }
    const nxt = timed[k + 1];
    if (nxt && nowMin >= cur.e && nowMin < nxt.s) {
      return { state: 'passing', prev: cur, next: nxt };
    }
  }
  return { state: 'after' };
}

function courseName(id) {
  if (!SCHED || id == null) return null;
  const c = SCHED.courses[String(id)];
  return c ? (c.name || '') : null;
}

// Upcoming work for one course. Undated items are kept — "no due date" is a
// real state and dropping it would hide an assignment entirely.
function courseWork(id, limit = 4) {
  if (!SCHED || id == null) return [];
  return (SCHED.assignments_by_course[String(id)] || []).slice(0, limit);
}

function dueLabel(a) {
  if (!a.due_at) return 'no due date';
  const today = SCHED ? SCHED.today : null;
  const day = String(a.due_at).slice(0, 10);
  let rel = day;
  if (today) {
    const d = Math.round(
      (Date.parse(`${day}T00:00:00`) - Date.parse(`${today}T00:00:00`)) / 86400000);
    if (d === 0) rel = 'today';
    else if (d === 1) rel = 'tomorrow';
    else if (d > 1 && d < 7) rel = `in ${d} days`;
  }
  // has_due_time is false for everything the iCal layer produced. Rendering a
  // date as a time would invent midnight — see memory/db.py on that column.
  if (a.has_due_time) {
    try {
      const t = new Date(a.due_at)
        .toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
      return `${rel} ${t}`;
    } catch { /* fall through to the bare day */ }
  }
  return rel;
}

function workList(id) {
  const work = courseWork(id);
  if (!work.length) {
    // Early in a semester most courses are empty. Say so — a blank slot reads
    // as a card that failed to load.
    return '<div class="pc-empty">Nothing posted.</div>';
  }
  return `<ul class="pc-work">${work.map((a) => `
    <li${a.submitted ? ' class="done"' : ''}>
      <span class="pc-work-title">${escapeHtml(a.title)}</span>
      <span class="pc-work-due mono">${escapeHtml(dueLabel(a))}</span>
    </li>`).join('')}</ul>`;
}

// How a slot names itself. An alternating slot on an unknown letter is TWO
// periods at one time and says so — "Period 4/5" reads as a period called
// four-slash-five, which is not a thing.
function periodLabel(p) {
  if (!p || !p.label) return 'Period';
  return `${p.alternating && !p.resolved ? 'Periods' : 'Period'} ${p.label}`;
}

// One slot's body: the course (or both, or none) plus its work.
//
// The unresolved branch shows both IDENTITIES, not both courses of one
// period — on an A day this hour is 4th, on a B day it is 5th, and there is
// no day with both. Without the number the two halves are indistinguishable
// once the letter badge is off screen.
function periodBody(p) {
  if (p.alternating && !p.resolved) {
    const both = (p.alternates || []).map((a) => {
      const n = courseName(a.course_id);
      return `
        <div class="pc-alt">
          <span class="pc-alt-letter mono">${escapeHtml(a.letter)}</span>
          <div class="pc-alt-body">
            <div class="pc-alt-n">Period ${escapeHtml(String(a.n == null ? '?' : a.n))}</div>
            <div class="pc-course-sm">${n ? escapeHtml(n) : '<span class="pc-none">unassigned</span>'}</div>
            ${n ? workList(a.course_id) : ''}
          </div>
        </div>`;
    }).join('');
    return `
      <div class="pc-unresolved">Rotation not set — showing both.</div>
      <div class="pc-alts">${both}</div>`;
  }
  const name = courseName(p.course_id);
  if (!name) {
    return '<div class="pc-course pc-none">No course assigned</div>'
      + '<div class="pc-empty">Set one on the Schedule page.</div>';
  }
  return `<div class="pc-course">${escapeHtml(name)}</div>${workList(p.course_id)}`;
}

function renderPeriodCard() {
  const card = document.getElementById('period-card');
  if (!card) return;
  if (!SCHED || !(SCHED.periods || []).length) { card.hidden = true; return; }
  card.hidden = false;

  const now = new Date();
  const nowMin = now.getHours() * 60 + now.getMinutes();
  const periods = SCHED.periods;

  // A manual view expires on its own so the card returns to the truth.
  if (VIEW_INDEX != null && Date.now() - VIEW_AT > VIEW_RETURN_MS) VIEW_INDEX = null;

  const loc = locateNow(periods, nowMin);

  // After the last bell the card becomes a different thing entirely.
  if (VIEW_INDEX == null && loc.state === 'after') {
    renderAfterSchool(card);
    return;
  }

  let shown = null;      // the period object being displayed
  let eyebrow = '';
  let timing = '';

  if (VIEW_INDEX != null) {
    shown = periods[VIEW_INDEX];
    eyebrow = periodLabel(shown);
    timing = `${minToLabel(hhmmToMin(shown.start))} – ${minToLabel(hhmmToMin(shown.end))}`;
  } else if (loc.state === 'in_period') {
    shown = loc.cur.p;
    eyebrow = periodLabel(shown);
    timing = `${fmtLeft(loc.cur.e - nowMin)} · ends ${minToLabel(loc.cur.e)}`;
  } else if (loc.state === 'passing') {
    shown = loc.next.p;
    eyebrow = 'Passing time';
    timing = `${periodLabel(shown)} starts in ${loc.next.s - nowMin} min`;
  } else if (loc.state === 'before') {
    shown = loc.next.p;
    eyebrow = 'Before first bell';
    timing = `${periodLabel(shown)} starts at ${minToLabel(loc.next.s)}`;
  }

  if (!shown) { card.hidden = true; return; }

  const idx = periods.indexOf(shown);
  const letter = SCHED.letter
    ? `<span class="pc-letter ${SCHED.letter_source === 'override' ? 'forced' : ''}">${
        escapeHtml(SCHED.letter)} DAY</span>`
    : '';
  const stale = staleNotice();

  card.innerHTML = `
    <div class="pc-head">
      <span class="pc-eyebrow">${escapeHtml(eyebrow)}</span>
      ${letter}
    </div>
    ${periodBody(shown)}
    <div class="pc-timing mono">${escapeHtml(timing)}</div>
    <div class="pc-nav">
      <button class="icon-btn" data-pc="prev" ${idx <= 0 ? 'disabled' : ''}>‹</button>
      <span class="pc-dots">${periods.map((p, i) =>
        `<span class="pc-dot${i === idx ? ' on' : ''}">${escapeHtml(p.label || '')}</span>`).join('')}</span>
      <button class="icon-btn" data-pc="next" ${idx >= periods.length - 1 ? 'disabled' : ''}>›</button>
      ${VIEW_INDEX != null ? '<button class="btn btn-outline pc-now" data-pc="now">Back to now</button>' : ''}
    </div>
    ${stale}
  `;

  card.querySelectorAll('[data-pc]').forEach((b) => {
    b.onclick = () => {
      const act = b.dataset.pc;
      if (act === 'now') { VIEW_INDEX = null; }
      else {
        VIEW_INDEX = Math.max(0, Math.min(periods.length - 1,
          idx + (act === 'next' ? 1 : -1)));
        VIEW_AT = Date.now();
      }
      renderPeriodCard();
    };
  });
}

// After the last bell the card stops being about periods and becomes a
// different thing: what is already spoken for, what is due, and what is left.
// Three sections ordered by how much choice there is about them.

let AFTER = null;          // last /api/after-school payload
let AFTER_PENDING = false;

function clockLabel(iso) {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  } catch { return ''; }
}

function spanLabel(mins) {
  const a = Math.abs(mins);
  const h = Math.floor(a / 60), m = a % 60;
  if (h && m) return `${h}h ${m}m`;
  if (h) return `${h}h`;
  return `${m}m`;
}

async function loadAfterSchool() {
  if (AFTER_PENDING) return;
  AFTER_PENDING = true;
  try {
    AFTER = await api.get('/api/after-school');
    AFTER._at = Date.now();
  } catch { /* keep the last payload rather than blanking the card */ }
  finally { AFTER_PENDING = false; }
  renderPeriodCard();
}

function renderAfterSchool(card) {
  if (!AFTER) {
    card.innerHTML = '<div class="pc-head"><span class="pc-eyebrow">After school</span></div>'
      + '<div class="pc-empty">Loading…</div>';
    loadAfterSchool();
    return;
  }

  const a = AFTER;

  const commitments = a.commitments.length
    ? `<ul class="pc-work">${a.commitments.map((c) => `
        <li>
          <span class="pc-work-title">${escapeHtml(c.title)}${
            c.location ? ` <span class="pc-loc">${escapeHtml(c.location)}</span>` : ''}</span>
          <span class="pc-work-due mono">${escapeHtml(clockLabel(c.start))}–${escapeHtml(clockLabel(c.end))}</span>
        </li>`).join('')}</ul>`
    : '<div class="pc-empty">Nothing on the calendar.</div>';

  const due = a.due.length
    ? `<ul class="pc-work">${a.due.map((d) => `
        <li>
          <span class="pc-work-title">${escapeHtml(d.title)}${
            d.course_name ? ` <span class="pc-loc">${escapeHtml(d.course_name)}</span>` : ''}</span>
          <span class="pc-work-due mono">${escapeHtml(dueLabelFor(d, a))}</span>
        </li>`).join('')}</ul>`
    : '<div class="pc-empty">Nothing due tonight or tomorrow.</div>';

  // THE HEADLINE. Negative is the case this card exists for, so it is loud
  // and it names the overrun rather than showing a minus sign.
  //
  // TWO DIFFERENT NEGATIVES, and they are not the same sentence. A commitment
  // running past bedtime is a scheduling problem; the clock already being
  // past bedtime is just late. Saying "your last commitment ends 11:07pm"
  // when there are no commitments is a card describing something that did not
  // happen, which is how a user learns to stop believing it.
  let free;
  if (a.overcommitted && a.commitments.length) {
    free = `<div class="pc-free over">
        <span class="pc-free-n">${escapeHtml(spanLabel(a.free_minutes))} past bedtime</span>
        <span class="pc-free-sub">Your last commitment ends ${escapeHtml(clockLabel(a.free_from))},
          after your ${escapeHtml(clockLabel(a.bedtime))} bedtime. No free time tonight.</span>
      </div>`;
  } else if (a.overcommitted) {
    free = `<div class="pc-free over">
        <span class="pc-free-n">${escapeHtml(spanLabel(a.free_minutes))} past bedtime</span>
        <span class="pc-free-sub">It is ${escapeHtml(clockLabel(a.now))} and bedtime was ${
          escapeHtml(clockLabel(a.bedtime))}.</span>
      </div>`;
  } else {
    free = `<div class="pc-free">
        <span class="pc-free-n">${escapeHtml(spanLabel(a.free_minutes))} free</span>
        <span class="pc-free-sub">From ${escapeHtml(clockLabel(a.free_from))} to ${
          escapeHtml(clockLabel(a.bedtime))}${
          a.committed_minutes ? `, after ${escapeHtml(spanLabel(a.committed_minutes))} of commitments` : ''}.</span>
      </div>`;
  }

  card.innerHTML = `
    <div class="pc-head">
      <span class="pc-eyebrow">After school</span>
      <span class="pc-letter">${escapeHtml(clockLabel(a.now))}</span>
    </div>
    ${free}
    <div class="pc-section">
      <div class="pc-section-h">Fixed commitments</div>
      ${commitments}
      ${a.calendar_error ? `<div class="pc-stale">Calendar unavailable — ${escapeHtml(a.calendar_error)}</div>` : ''}
    </div>
    <div class="pc-section">
      <div class="pc-section-h">Due tonight or tomorrow</div>
      ${due}
    </div>
    <div class="pc-section">
      <div class="pc-section-h">Add a commitment</div>
      <div class="quick-add">
        <input class="input" id="qa-title" placeholder="What is it?">
        <input class="input" id="qa-date" type="date" value="${escapeAttr((a.now || '').slice(0, 10))}">
        <input class="input" id="qa-start" type="time">
        <input class="input" id="qa-end" type="time">
        <button class="btn btn-primary" id="qa-add">Add</button>
      </div>
      <div class="hint" id="qa-result">Goes through the same confirmation card as everything else.</div>
    </div>
    <div class="pc-nav">
      <button class="icon-btn" data-pc="reload" title="Refresh">⟳</button>
      <span class="pc-dots"></span>
      <button class="btn btn-outline pc-now" data-pc="back">Today's periods</button>
    </div>
    ${staleNotice()}
  `;

  card.querySelector('[data-pc="back"]').onclick = () => {
    VIEW_INDEX = 0; VIEW_AT = Date.now(); renderPeriodCard();
  };
  card.querySelector('[data-pc="reload"]').onclick = () => loadAfterSchool();
  wireQuickAdd(card);
}

// The quick-add field. It POSTs and then STOPS — the confirmation card is
// what happens next, and it arrives on the stream like every other card. This
// deliberately does not write anything, report success, or refresh the
// commitments list: nothing has been written yet at that point, and a card
// that said "added" before the tap would be the one lie this whole gate
// exists to prevent.
function wireQuickAdd(card) {
  const btn = card.querySelector('#qa-add');
  const out = card.querySelector('#qa-result');
  if (!btn) return;
  btn.onclick = async () => {
    const title = card.querySelector('#qa-title').value.trim();
    if (!title) { out.textContent = 'Give it a name first.'; return; }
    btn.disabled = true;
    out.textContent = 'Proposing…';
    try {
      await api.post('/api/commitment', {
        title,
        date: card.querySelector('#qa-date').value,
        start_time: card.querySelector('#qa-start').value,
        end_time: card.querySelector('#qa-end').value,
      });
      card.querySelector('#qa-title').value = '';
      out.textContent = 'Confirmation card sent — approve it in Chat or Telegram.';
      flash('CARD SENT');
    } catch (e) {
      out.textContent = e.detail || e.message;
    } finally {
      btn.disabled = false;
    }
  };
}

// The after-school payload carries its own `today`, so dueLabel's SCHED-based
// relative math is given the right anchor rather than assuming one is loaded.
function dueLabelFor(d, payload) {
  const saved = SCHED;
  try {
    SCHED = SCHED || { today: (payload.now || '').slice(0, 10), courses: {}, assignments_by_course: {} };
    return dueLabel(d);
  } finally { SCHED = saved; }
}

// Said out loud rather than shown silently: a card confidently rendering
// hours-old homework is worse than one that admits its age.
function staleNotice() {
  const c = (SCHED && SCHED.cache) || {};
  if (!c.refreshed_at) return '';
  const age = (Date.now() - Date.parse(c.refreshed_at)) / 60000;
  if (!(age > 45)) return '';
  return `<div class="pc-stale">Canvas last read ${escapeHtml(fmtRelative(c.refreshed_at))}.</div>`;
}

async function loadSchedule() {
  try {
    SCHED = await api.get('/api/schedule');
  } catch { /* keep the last payload; the card keeps working offline */ }
  renderPeriodCard();
}

function startPeriodCard() {
  AFTER = null;          // a stale free-time number is worse than a spinner
  loadSchedule();
  if (PERIOD_TIMER) clearInterval(PERIOD_TIMER);
  // Local math only — no request. Twenty seconds is under the smallest
  // meaningful unit the card shows (a minute) without being a busy loop.
  PERIOD_TIMER = setInterval(() => {
    renderPeriodCard();
    // The free-time math counts down against the wall clock, so it does need
    // periodic re-reading — but every five minutes, not every tick.
    if (AFTER && Date.now() - (AFTER._at || 0) > 300000) {
      AFTER._at = Date.now();
      loadAfterSchool();
    }
  }, 20000);
}

// The card's stream subscription is registered further down, beside the other
// onStream calls — NOT here. STREAM_HANDLERS is a `const` declared in the Live
// stream section below, so calling onStream() from this point in the file
// reads it inside its temporal dead zone and throws at load, which aborts the
// whole script and renders a blank dashboard. Registration order does not
// matter; position relative to that declaration does.

// ── Schedule ───────────────────────────────────────────────────────────
//
// The setup flow for the period card: bell times, a Canvas course per period,
// and the A/B rotation's anchor. Written once and then forgotten.
//
// Period assignments are ordinary config edits — mutate CONFIG, POST the whole
// document — exactly like the GroupMe group cards and the gcal rows above.
// The A/B override is NOT, and goes to its own endpoint: an override is only
// ever for today, and the server stamps the date so a client cannot set one
// that never expires.

let COURSE_CACHE = [];   // [{id, name, course_code, source, fetched_at}]

// "IB BIOLOGY 1-RYALW" is what the iCal feed gives; a real course_code (REST)
// is shorter and is what you would actually recognise, so it leads when
// present. Both are shown — the code alone is ambiguous across years.
function courseLabel(c) {
  if (!c) return '(unknown course)';
  return c.course_code ? `${c.course_code} — ${c.name}` : c.name;
}

function courseOption(selectedId) {
  const opts = ['<option value="">— none —</option>'];
  for (const c of COURSE_CACHE) {
    const sel = String(c.id) === String(selectedId) ? ' selected' : '';
    opts.push(`<option value="${escapeAttr(c.id)}"${sel}>${escapeHtml(courseLabel(c))}</option>`);
  }
  return opts.join('');
}

async function renderSchedule() {
  const s = (CONFIG.schedule = CONFIG.schedule || {});
  s.periods = s.periods || [];
  s.ab_cycle = s.ab_cycle || { pattern: ['A', 'B'] };

  bindInput(document.getElementById('sched-bedtime'), 'schedule.bedtime');

  const hint = document.getElementById('courses-hint');
  try {
    const r = await api.get('/api/canvas/courses');
    COURSE_CACHE = r.courses || [];
    renderCourseList(r.cache || {});
    hint.innerHTML = COURSE_CACHE.length
      ? `${COURSE_CACHE.length} course${COURSE_CACHE.length === 1 ? '' : 's'} from Canvas${
          r.cache && r.cache.refreshed_at ? `, as of ${escapeHtml(fmtRelative(r.cache.refreshed_at))}` : ''}`
      : 'No courses cached yet — Friday refreshes Canvas every 15 minutes.';
    // The REST layer is what supplies course codes, due times and submission
    // status. Say so plainly rather than silently rendering a thinner card.
    if (r.cache && !r.cache.rest_ok) {
      hint.innerHTML += ' <span class="warn">· Canvas API token expired or unset —'
        + ' course codes and due times unavailable, due dates still work.</span>';
    }
  } catch (e) {
    hint.textContent = `Could not load courses: ${e.message}`;
  }

  renderPeriodRows();
  renderAbCycle();
}

function renderCourseList(cache) {
  const box = document.getElementById('course-list');
  if (!box) return;
  box.innerHTML = '';
  for (const c of COURSE_CACHE) {
    const row = document.createElement('div');
    row.className = 'course-row';
    row.innerHTML = `
      <span class="course-name">${escapeHtml(c.name)}</span>
      <span class="course-code mono">${escapeHtml(c.course_code || '—')}</span>
      <span class="course-id mono">${escapeHtml(c.id)}</span>
    `;
    box.appendChild(row);
  }
}

// Bell times that overlap are not a config the card can render sensibly —
// two slots claiming 10:50 makes "what period is it" depend on sort order.
// Said out loud in the setup page rather than silently resolved, because the
// migration from the old two-period rotation produces exactly this and the
// real time is the user's to supply.
function overlapWarnings(periods) {
  const timed = periods
    .map((p, i) => ({ i, s: hhmmToMin(p.start), e: hhmmToMin(p.end) }))
    .filter((x) => x.s != null && x.e != null)
    .sort((a, b) => a.s - b.s);
  const bad = new Set();
  for (let k = 1; k < timed.length; k++) {
    if (timed[k].s < timed[k - 1].e) { bad.add(timed[k].i); bad.add(timed[k - 1].i); }
  }
  return bad;
}

// One row per SLOT. The alternating slot is one time and two identities —
// a period number and a course for each letter — so it renders as one row
// with two sub-rows, never as two rows. Two rows would let the times drift
// apart, which is the state the model exists to make unrepresentable.
function renderPeriodRows() {
  const box = document.getElementById('period-rows');
  if (!box) return;
  box.innerHTML = '';
  const periods = CONFIG.schedule.periods || [];
  const overlapping = overlapWarnings(periods);

  periods.forEach((p, pi) => {
    const alts = Array.isArray(p.alternates) ? p.alternates : null;
    const row = document.createElement('div');
    row.className = 'period-row' + (alts ? ' alternating' : '');
    const label = alts
      ? alts.map((a) => (a.n == null ? '?' : a.n)).join('/')
      : String(p.n == null ? '?' : p.n);
    row.innerHTML = `
      <span class="period-n mono">${escapeHtml(label)}</span>
      <input type="time" class="input period-start" value="${escapeAttr(p.start || '')}">
      <span class="period-dash">–</span>
      <input type="time" class="input period-end" value="${escapeAttr(p.end || '')}">
      <div class="period-courses">
        ${alts
          ? alts.map((a, ai) => `
             <div class="period-alt">
               <label class="alt-label">${escapeHtml(String(a.letter || ''))} DAY</label>
               <input type="number" class="input period-alt-n" data-alt="${ai}"
                      min="1" max="12" value="${escapeAttr(a.n == null ? '' : String(a.n))}">
               <select class="input period-course" data-alt="${ai}">${courseOption(a.canvas_course)}</select>
             </div>`).join('')
          : `<select class="input period-course">${courseOption(p.canvas_course)}</select>`}
      </div>
      ${overlapping.has(pi) ? '<div class="period-warn">Overlaps another slot — two periods cannot share the clock.</div>' : ''}
    `;

    row.querySelector('.period-start').onblur = async (e) => {
      p.start = e.target.value; await saveConfig(); renderPeriodRows();
    };
    row.querySelector('.period-end').onblur = async (e) => {
      p.end = e.target.value; await saveConfig(); renderPeriodRows();
    };
    row.querySelectorAll('.period-alt-n').forEach((inp) => {
      inp.onblur = async () => {
        const v = inp.value.trim();
        alts[Number(inp.dataset.alt)].n = v === '' ? null : Number(v);
        await saveConfig();
        renderPeriodRows();
      };
    });
    row.querySelectorAll('.period-course').forEach((sel) => {
      sel.onchange = async () => {
        const v = sel.value || null;
        if (alts) alts[Number(sel.dataset.alt)].canvas_course = v;
        else p.canvas_course = v;
        await saveConfig();
      };
    });
    box.appendChild(row);
  });
}


function renderAbCycle() {
  const cycle = CONFIG.schedule.ab_cycle;
  const startEl = document.getElementById('ab-start');
  startEl.value = cycle.start_date || '';
  startEl.onblur = async () => {
    cycle.start_date = startEl.value || null;
    await saveConfig();
    refreshAbStatus();
  };

  document.querySelectorAll('#ab-override [data-letter]').forEach((b) => {
    b.onclick = async () => {
      try {
        const r = await api.post('/api/schedule/override', {
          letter: b.dataset.letter === 'clear' ? null : b.dataset.letter,
        });
        // The server owns the date stamp, so the local copy is refreshed from
        // its answer rather than assumed.
        cycle.manual_override = r.override;
        flash('SAVED');
        refreshAbStatus();
      } catch (e) {
        flash(e.detail || 'OVERRIDE FAILED', true);
      }
    };
  });
  refreshAbStatus();
}

async function refreshAbStatus() {
  const out = document.getElementById('ab-status');
  if (!out) return;
  let s;
  try { s = await api.get('/api/schedule'); }
  catch { out.textContent = ''; return; }

  const cycle = CONFIG.schedule.ab_cycle;
  cycle.manual_override = (s.schedule.ab_cycle || {}).manual_override || null;

  document.querySelectorAll('#ab-override [data-letter]').forEach((b) => {
    const active = s.letter_source === 'override'
      ? b.dataset.letter === s.letter
      : b.dataset.letter === 'clear';
    b.classList.toggle('active', active);
  });

  if (s.letter_source === 'override') {
    out.innerHTML = `Today is forced to <strong>${escapeHtml(s.letter)}</strong>. Clears at midnight.`;
  } else if (s.letter_source === 'counted') {
    out.innerHTML = `Today is a <strong>${escapeHtml(s.letter)}</strong> day, counted from the first A day.`;
  } else if (!s.is_school_day) {
    out.textContent = 'Weekend — no letter today.';
  } else {
    out.innerHTML = 'No first A day set, so the rotation is unresolved. '
      + '4th and 5th show <strong>both</strong> courses, labeled.';
  }
}

// ── Calendar ───────────────────────────────────────────────────────────

let GCAL_SYNC_STATUS = {};   // calendar name → most-recent sync ISO timestamp

function renderCalendar() {
  bindInput(document.getElementById('default-calendar'), 'agent.default_calendar');
  renderGcalRows();
  // Pull last-sync timestamps, then re-render so the .last-sync slots populate.
  api.get('/api/calendar/sync-status').then((r) => {
    GCAL_SYNC_STATUS = (r && r.last_sync) || {};
    renderGcalRows();
  }).catch(() => {});
  document.getElementById('gcal-add').onclick = async () => {
    const list = (CONFIG.gcal_sync = CONFIG.gcal_sync || {});
    list.calendars = list.calendars || [];
    list.calendars.push({ name: '', ical_url: '' });
    renderGcalRows();
    await saveConfig();
  };
}

function renderGcalRows() {
  const container = document.getElementById('gcal-list');
  container.innerHTML = '';
  const list = (CONFIG.gcal_sync && CONFIG.gcal_sync.calendars) || [];
  list.forEach((cal, idx) => {
    const row = document.createElement('div');
    row.className = 'gcal-row';
    const synced = GCAL_SYNC_STATUS[cal.name];
    row.innerHTML = `
      <input class="input" placeholder="Calendar name" value="${escapeAttr(cal.name || '')}">
      <div class="gcal-url">
        <input class="input gcal-url-input" placeholder="https://calendar.google.com/calendar/ical/..." readonly>
        <button class="icon-btn gcal-reveal" title="Show / hide URL">👁</button>
      </div>
      <span class="last-sync${synced ? '' : ' never'}" title="Most recent Google → Apple sync">${
        synced ? `synced ${escapeHtml(fmtRelative(synced))}` : 'never synced'
      }</span>
      <button class="icon-btn" title="Remove">×</button>
    `;
    const nameEl = row.querySelector('input.input:not(.gcal-url-input)');
    const urlEl = row.querySelector('.gcal-url-input');

    // URL is masked + readonly by default; the eye toggles a revealed, editable
    // state (matching how secret tokens are handled elsewhere).
    let revealed = false;
    const paint = () => {
      urlEl.value = revealed ? (cal.ical_url || '') : maskUrl(cal.ical_url || '');
      urlEl.readOnly = !revealed;
    };
    paint();
    row.querySelector('.gcal-reveal').onclick = () => {
      // Persist any in-progress edit before hiding so it isn't lost.
      if (revealed) cal.ical_url = urlEl.value;
      revealed = !revealed;
      paint();
    };

    nameEl.onblur = async () => { cal.name = nameEl.value; await saveConfig(); };
    urlEl.onblur = async () => {
      if (!revealed) return;            // never persist the masked display value
      cal.ical_url = urlEl.value;
      await saveConfig();
    };
    row.querySelector('button.icon-btn[title="Remove"]').onclick = async () => {
      list.splice(idx, 1);
      renderGcalRows();
      await saveConfig();
    };
    container.appendChild(row);
  });
}

// ── Notifications ──────────────────────────────────────────────────────

function renderNotifications() {
  const n = (CONFIG.notifications = CONFIG.notifications || {});
  n.morning_briefing = n.morning_briefing || { enabled: true, time: '07:00' };
  n.evening_briefing = n.evening_briefing || { enabled: true, time: '20:00' };
  n.reminder_thresholds = n.reminder_thresholds || [5, 3, 1];

  bindInput(document.getElementById('morning-time'),    'notifications.morning_briefing.time');
  bindInput(document.getElementById('morning-enabled'), 'notifications.morning_briefing.enabled');
  bindInput(document.getElementById('evening-time'),    'notifications.evening_briefing.time');
  bindInput(document.getElementById('evening-enabled'), 'notifications.evening_briefing.enabled');
  bindInput(document.getElementById('proactive'),       'notifications.proactive_reminders');
  bindInput(document.getElementById('urgent'),          'notifications.urgent_interrupts');
  bindInput(document.getElementById('canvas-poll'),     'notifications.canvas_polling');
  bindInput(document.getElementById('groupme-poll'),    'notifications.groupme_polling');

  const set = new Set(n.reminder_thresholds.map(Number));
  document.querySelectorAll('.checkbox-row input[data-day]').forEach((cb) => {
    cb.checked = set.has(parseInt(cb.dataset.day, 10));
    cb.onchange = async () => {
      const days = [...document.querySelectorAll('.checkbox-row input[data-day]:checked')]
        .map((x) => parseInt(x.dataset.day, 10))
        .sort((a, b) => b - a);
      n.reminder_thresholds = days;
      await saveConfig();
    };
  });
}

// ── Voice ──────────────────────────────────────────────────────────────

function renderVoice() {
  const v = (CONFIG.voice = CONFIG.voice || {});

  bindInput(document.getElementById('v-enabled'),       'voice.enabled');
  bindInput(document.getElementById('v-mic'),           'voice.mic_enabled');
  bindInput(document.getElementById('v-wake'),          'voice.wake_enabled');
  bindInput(document.getElementById('v-clap'),          'voice.clap_enabled');
  bindInput(document.getElementById('v-always-speak'),  'voice.always_speak');
  bindInput(document.getElementById('v-ptt-key'),       'voice.push_to_talk_key');

  const whisperSel = document.getElementById('v-whisper');
  whisperSel.value = v.whisper_model || 'base';
  whisperSel.onchange = async () => {
    set(CONFIG, 'voice.whisper_model', whisperSel.value);
    await saveConfig();
  };

  const pulse = document.getElementById('voice-pulse');
  const text = document.getElementById('voice-status');
  const sessionEl = document.getElementById('voice-session-state');

  async function tick() {
    let s;
    try { s = await api.get('/api/voice/status'); }
    catch { return; }
    pulse.className = 'pulse';
    text.className = 'hero-text';
    if (s.listening) {
      text.textContent = 'LISTENING';
      pulse.classList.add('online');
    } else if (s.agent_loaded) {
      text.textContent = 'ONLINE';
      pulse.classList.add('online');
    } else {
      text.textContent = 'OFFLINE';
      text.classList.add('offline');
      pulse.classList.add('offline');
    }
    sessionEl.textContent = s.session_present ? 'present' : 'missing';
    sessionEl.style.color = s.session_present ? 'var(--teal)' : 'var(--danger)';
  }

  tick();
  VOICE_TIMER = setInterval(tick, 2000);

  document.getElementById('voice-restart').onclick = async () => {
    try { await api.post('/api/voice/restart'); flash('RESTARTING VOICE'); }
    catch { flash('FAILED', true); }
  };

  const modal = document.getElementById('voice-logs-modal');
  document.getElementById('voice-logs-btn').onclick = async () => {
    const body = document.getElementById('voice-logs-body');
    body.textContent = 'loading...';
    modal.classList.remove('hidden');
    try {
      const r = await api.get('/api/voice/logs?lines=200');
      body.textContent = (r.lines || []).join('\n');
      body.scrollTop = body.scrollHeight;
    } catch (e) {
      body.textContent = `Error: ${e.message}`;
    }
  };
  document.getElementById('voice-logs-close').onclick = () => modal.classList.add('hidden');
}

// ── About ──────────────────────────────────────────────────────────────

function renderAbout() {
  // Server URL + last-update from live status (last update = when the running
  // process last started, i.e. the last restart/redeploy of the agent).
  document.getElementById('about-server').textContent = location.origin;
  api.get('/api/status').then((s) => {
    const row = document.getElementById('about-update-row');
    const val = document.getElementById('about-update');
    if (s && s.started_at) {
      val.textContent = `${fmtDate(s.started_at)} (${fmtRelative(s.started_at)})`;
      row.classList.remove('hidden');
    }
  }).catch(() => {});

  document.getElementById('about-restart').onclick = async () => {
    try { await api.post('/api/friday/restart'); flash('RESTARTING'); }
    catch { flash('FAILED', true); }
  };
  const modal = document.getElementById('logs-modal');
  document.getElementById('about-logs').onclick = async () => {
    const body = document.getElementById('logs-body');
    body.textContent = 'loading...';
    modal.classList.remove('hidden');
    try {
      const r = await api.get('/api/logs?lines=200');
      body.textContent = (r.lines || []).join('\n');
      body.scrollTop = body.scrollHeight;
    } catch (e) {
      body.textContent = `Error: ${e.message}`;
    }
  };
  document.getElementById('logs-close').onclick = () => modal.classList.add('hidden');
}

// ── Reusable helpers ───────────────────────────────────────────────────

function setupSegmented(id, value, onPick) {
  const seg = document.getElementById(id);
  seg.querySelectorAll('button').forEach((b) => {
    b.classList.toggle('active', b.dataset.value === value);
    b.onclick = async () => {
      seg.querySelectorAll('button').forEach((x) => x.classList.toggle('active', x === b));
      await onPick(b.dataset.value);
    };
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function escapeAttr(s) { return escapeHtml(s); }



// ── Live stream ────────────────────────────────────────────────────────
//
// One EventSource for the whole page, opened at boot rather than by the chat
// view: a card resolved in Telegram has to reach a browser sitting on the
// Today page, and an interrupt has to arrive whether or not the user is
// looking at the transcript.
//
// Reconnection is EventSource's own — it retries by itself when the Mac sleeps
// or the Wi-Fi drops, which is most of why this is SSE. Nothing here replays:
// every event is also in the database, and a browser that missed one recovers
// by reading. See dashboard/stream.py.

let STREAM = null;
const STREAM_HANDLERS = {};

// ── Interrupts ─────────────────────────────────────────────────────────
//
// The server also raises a macOS notification for every interrupt (see
// channels/dashboard.py), which is what reaches the user when this app is not
// running at all. This one exists for the case that one cannot cover: a
// notification that CLICKS THROUGH. `osascript display notification` is
// attributed to osascript, so clicking it activates that; a Notification
// raised here belongs to this window, and onclick can focus it.
//
// Permission is requested on a user gesture — sending a message — because
// browsers refuse the request outside one, and a refusal is permanent for the
// origin. Asking at page load would burn the one chance on a page the user has
// not decided they care about yet.

function notifyPermissionMaybe() {
  if (!('Notification' in window)) return;
  if (Notification.permission !== 'default') return;
  try { Notification.requestPermission(); } catch { /* older API shape */ }
}

function raiseNotification(title, body) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  try {
    const n = new Notification(title || 'Friday', {
      body: body || '',
      icon: '/static/icon-192.png',
      // Collapses repeats rather than stacking. An interrupt the user has not
      // acted on does not become more true by arriving twice.
      tag: 'friday-interrupt',
    });
    n.onclick = () => { window.focus(); n.close(); };
  } catch (e) { /* a blocked notification is not a broken page */ }
}

onStream('notify', (ev) => raiseNotification(ev.title, ev.text));

// Canvas moved. Re-read rather than trusting the event to carry the data —
// both payloads, since the after-school due list comes from the same cache.
onStream('canvas', () => {
  if (CURRENT_ROUTE !== 'today') return;
  loadSchedule();
  if (AFTER) loadAfterSchool();
});

function onStream(kind, fn) {
  (STREAM_HANDLERS[kind] = STREAM_HANDLERS[kind] || []).push(fn);
}

function openStream() {
  if (STREAM) return;
  try {
    STREAM = new EventSource('/api/stream');
  } catch (e) {
    return;   // No stream is a degraded page, not a broken one.
  }
  STREAM.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    (STREAM_HANDLERS[ev.kind] || []).forEach((fn) => {
      try { fn(ev); } catch (e) { /* one bad handler must not kill the stream */ }
    });
  };
  STREAM.onerror = () => {
    // EventSource reconnects on its own; this exists so the failure is
    // visible in the console rather than silent.
    console.warn('stream disconnected — retrying');
  };
}

// ── Chat ───────────────────────────────────────────────────────────────
//
// A rendering surface and nothing else. Every message goes to POST /api/chat,
// which runs the same channels/conversation.py::handle() the Telegram handler
// runs — same gate, same history, same effects. Nothing here decides what
// Friday says, and nothing here talks to the model.

function chatLine(role, text, channel, at) {
  const div = document.createElement('div');
  div.className = `chat-line chat-${role === 'user' ? 'user' : 'friday'}`;
  const from = channel && channel !== 'dashboard'
    ? `<span class="chat-via mono">${escapeHtml(channel)}</span>` : '';
  div.innerHTML =
    `<div class="chat-bubble">${escapeHtml(text).replace(/\n/g, '<br>')}</div>` +
    `<div class="chat-meta mono">${escapeHtml(chatTime(at))}${from}</div>`;
  return div;
}

function chatTime(at) {
  if (!at) return '';
  try {
    return new Date(at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  } catch { return ''; }
}

function chatScroll() {
  const log = document.getElementById('chat-log');
  if (log) log.scrollTop = log.scrollHeight;
}

function chatAppend(node) {
  const log = document.getElementById('chat-log');
  if (!log) return;
  log.appendChild(node);
  chatScroll();
}

// A dashboard event from POST /api/chat. `kind` is the channel's own
// vocabulary (channels/dashboard.py) — switched on rather than sniffed,
// because a typo here would silently drop a message.
function chatRenderEvent(ev) {
  if (chatAlreadyRendered(ev)) return;
  if (ev.kind === 'card') { chatAppend(chatCard(ev.key, ev.text)); return; }
  const text = ev.kind === 'notify'
    ? [ev.title, ev.text].filter(Boolean).join(' — ') : (ev.text || '');
  if (text) chatAppend(chatLine('assistant', text, 'dashboard', ev.at));
}

// Events arriving on the stream, as opposed to on this browser's own POST
// response. Rendered only when the chat view is mounted — a message that
// arrives while the user is on Settings is in the transcript, which the view
// reads when it opens.
onStream('message', (ev) => { if (CURRENT_ROUTE === 'chat') chatRenderEvent(ev); });
onStream('notify',  (ev) => { if (CURRENT_ROUTE === 'chat') chatRenderEvent(ev); });
onStream('card',    (ev) => { if (CURRENT_ROUTE === 'chat') chatRenderEvent(ev); });

// Anything this browser rendered from its own POST response must not be
// rendered a second time when the same event arrives on the stream. Keyed on
// the timestamp the channel stamped, which is the same value in both copies.
const CHAT_SEEN = new Set();

function chatAlreadyRendered(ev) {
  const key = `${ev.kind}:${ev.at}:${ev.text || ev.title || ''}`;
  if (CHAT_SEEN.has(key)) return true;
  CHAT_SEEN.add(key);
  if (CHAT_SEEN.size > 400) CHAT_SEEN.clear();   // bounded; it is a dedupe, not a log
  return false;
}

async function chatSend(text) {
  const input = document.getElementById('chat-input');
  const send = document.getElementById('chat-send');
  input.disabled = true; send.disabled = true;
  chatAppend(chatLine('user', text, 'dashboard', new Date().toISOString()));
  const thinking = document.createElement('div');
  thinking.className = 'chat-line chat-friday chat-thinking';
  thinking.innerHTML = '<div class="chat-bubble">…</div>';
  chatAppend(thinking);
  try {
    const r = await api.post('/api/chat', { text });
    thinking.remove();
    if (r.paused) {
      chatAppend(chatLine('assistant', 'Paused — nothing was processed.',
                          'dashboard', new Date().toISOString()));
    }
    (r.events || []).forEach(chatRenderEvent);
  } catch (e) {
    thinking.remove();
    // A transport failure, not a model failure — the model's own errors come
    // back as ordinary messages with an error_kind. Worth telling apart.
    chatAppend(chatLine('assistant',
                        `Couldn't reach Friday: ${e.detail || e.message}`,
                        'dashboard', new Date().toISOString()));
  } finally {
    input.disabled = false; send.disabled = false; input.focus();
  }
}


// ── Permission cards, in the chat ──────────────────────────────────────
//
// The SAME pending_actions keys Telegram's inline buttons carry. A card
// confirmed here and a card confirmed there resolve the same row, run the
// same stored arguments through the same executor, and cannot both execute —
// the second tap, on either surface, is refused with a message.
//
// The text is the server's, verbatim. No persona, no quip, nothing prepended
// (invariants 3 and 6). This function chooses buttons and nothing else.

const CHAT_CARDS = new Map();       // pending key → element

function chatCard(key, text) {
  const el = document.createElement('div');
  el.className = 'chat-line chat-friday chat-card-line';
  el.innerHTML = `
    <div class="chat-card">
      <div class="chat-card-text">${escapeHtml(text || '').replace(/\n/g, '<br>')}</div>
      <div class="chat-card-actions">
        <button class="btn btn-primary btn-sm" data-act="confirm">Confirm</button>
        <button class="btn btn-outline btn-sm" data-act="cancel">Cancel</button>
      </div>
      <div class="chat-card-status mono hidden"></div>
    </div>`;
  el.querySelector('[data-act="confirm"]').onclick = () => chatResolve(key, 'confirm', el);
  el.querySelector('[data-act="cancel"]').onclick  = () => chatResolve(key, 'cancel', el);
  if (key) CHAT_CARDS.set(key, el);
  return el;
}

function chatCardSettle(el, label) {
  const actions = el.querySelector('.chat-card-actions');
  const status = el.querySelector('.chat-card-status');
  if (actions) actions.remove();
  if (status) { status.textContent = label; status.classList.remove('hidden'); }
  el.classList.add('chat-card-resolved');
}

async function chatResolve(key, verb, el) {
  el.querySelectorAll('button').forEach((b) => { b.disabled = true; });
  try {
    const r = await api.post(`/api/pending-approvals/${key}/${verb}`);
    // A stale card is re-proposed rather than run: the row is superseded and a
    // NEW card arrives in the events below. Settling this one as "replaced"
    // rather than "confirmed" is the whole point of that mechanism being
    // visible — the user has not approved anything yet.
    const label = { confirmed: 'CONFIRMED', cancelled: 'CANCELLED',
                    stale: 'REPLACED', superseded: 'REPLACED',
                    expired: 'EXPIRED' }[r.status] || (r.status || '').toUpperCase();
    chatCardSettle(el, label);
    (r.events || []).forEach(chatRenderEvent);
    if (key) CHAT_CARDS.delete(key);
  } catch (e) {
    // 409 is the cross-channel case: resolved in Telegram while this button
    // was still on screen. It fails closed server-side; here it has to stop
    // looking live.
    chatCardSettle(el, 'ALREADY RESOLVED');
    chatAppend(chatLine('assistant',
                        e.detail || "That one's already been dealt with, sir.",
                        'dashboard', new Date().toISOString()));
    if (key) CHAT_CARDS.delete(key);
  }
}

// Cards resolved somewhere else. The dashboard's own resolutions arrive here
// too, harmlessly — the element is already out of the map by then.
onStream('pending', (ev) => {
  const el = CHAT_CARDS.get(ev.id);
  if (!el) return;
  chatCardSettle(el, (ev.status || 'resolved').toUpperCase());
  CHAT_CARDS.delete(ev.id);
});

// A card resolved by tapping the button IN TELEGRAM is resolved inside
// effects/pending.py, which must not know a dashboard exists — so there is no
// event to listen for. This poll is what closes that direction, and it is the
// honest cost of not cross-wiring two channels together.
let CHAT_PENDING_TIMER = null;

async function chatSyncPending() {
  if (!CHAT_CARDS.size) return;
  try {
    const r = await api.get('/api/pending-approvals');
    const live = new Set((r.pending || []).map((p) => p.id));
    for (const [key, el] of [...CHAT_CARDS]) {
      if (!live.has(key)) { chatCardSettle(el, 'RESOLVED ELSEWHERE'); CHAT_CARDS.delete(key); }
    }
  } catch { /* a failed poll is a stale card, not a broken page */ }
}

async function renderChat() {
  const log = document.getElementById('chat-log');
  const form = document.getElementById('chat-form');
  const input = document.getElementById('chat-input');
  try {
    const r = await api.get('/api/chat/history');
    log.innerHTML = '';
    if (!(r.messages || []).length) {
      log.innerHTML = '<div class="hint">Nothing yet. Say something.</div>';
    }
    (r.messages || []).forEach((m) => {
      log.appendChild(chatLine(m.role, m.content, m.channel, m.at));
    });
    chatScroll();
  } catch (e) {
    log.innerHTML = `<div class="hint">Couldn't load the transcript: ${escapeHtml(e.message)}</div>`;
  }
  // Cards already waiting — proposed before this tab was opened, or on the
  // other surface. Rendered after the transcript because they are the live
  // question, not part of the history.
  CHAT_CARDS.clear();
  try {
    const p = await api.get('/api/pending-approvals');
    (p.pending || []).forEach((row) => {
      if (row.action_type === 'tool_call') {
        chatAppend(chatCard(row.id, row.proposal));
      }
    });
  } catch { /* the transcript is still worth showing without them */ }

  if (CHAT_PENDING_TIMER) clearInterval(CHAT_PENDING_TIMER);
  CHAT_PENDING_TIMER = setInterval(chatSyncPending, 5000);

  form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    notifyPermissionMaybe();
    const hint = log.querySelector('.hint');
    if (hint) hint.remove();
    chatSend(text);
  });
  input.focus();
}

// ── Boot ───────────────────────────────────────────────────────────────

async function boot() {
  try {
    // Sensitive fields need the real values to render in their <input>s. Local-only
    // server, so we just reveal them all on initial load.
    CONFIG = await api.get('/api/config?reveal=1');
  } catch (e) {
    PAGE.innerHTML = `<div class="panel"><h2>CONNECTION ERROR</h2><div class="hint">${e.message}</div></div>`;
    return;
  }
  updateSidebar();
  openStream();
  const initial = (location.hash.replace('#/', '') || 'today');
  navigate(initial);
}

boot();
