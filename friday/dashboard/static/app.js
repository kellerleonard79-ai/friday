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
  ai: renderAI,
  persona: renderPersona,
  integrations: renderIntegrations,
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
    const dr = row.draft || {};
    const card = document.createElement('div');
    card.className = 'pending-card';
    const when = [dr.date, dr.start_time].filter(Boolean).join(' ')
      + (dr.end_time ? `–${dr.end_time}` : '');
    card.innerHTML = `
      <div class="pending-summary">
        <div class="pending-title">${escapeHtml(dr.title || '(untitled)')}</div>
        <div class="pending-meta mono">${escapeHtml(when || '(all day)')} · ${escapeHtml(dr.calendar || 'default')}</div>
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
      </div>
    `;
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
    host.appendChild(card);
  }
}

async function pendingAction(id, verb) {
  try {
    const r = await api.post(`/api/pending-approvals/${id}/${verb}`);
    if (verb === 'confirm') flash(r.ok ? 'CONFIRMED' : 'WRITE FAILED', !r.ok);
    else flash('CANCELLED');
    PENDING_SIG = null;   // force refetch
    PENDING_EDITING = false;
  } catch { flash('FAILED', true); }
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
  const initial = (location.hash.replace('#/', '') || 'today');
  navigate(initial);
}

boot();
