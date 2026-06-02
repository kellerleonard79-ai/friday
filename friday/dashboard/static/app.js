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
    if (!r.ok) {
      const t = await r.text().catch(() => '');
      throw new Error(`${path} → ${r.status} ${t.slice(0, 200)}`);
    }
    return r.json();
  },
};

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
  status: renderStatus,
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
  if (!ROUTES[route]) route = 'status';
  if (CURRENT_ROUTE === route) return;
  CURRENT_ROUTE = route;
  if (STATUS_TIMER) { clearInterval(STATUS_TIMER); STATUS_TIMER = null; }
  if (VOICE_TIMER) { clearInterval(VOICE_TIMER); VOICE_TIMER = null; }
  document.querySelectorAll('.nav-item').forEach((a) => {
    a.classList.toggle('active', a.dataset.route === route);
  });
  const tpl = document.getElementById(`page-${route}`);
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
  const r = (location.hash.replace('#/', '') || 'status');
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

async function renderStatus() {
  const heroText = document.getElementById('hero-status');
  const heroPulse = document.getElementById('hero-pulse');
  const pauseBtn = document.getElementById('pause-btn');

  async function tick() {
    let s;
    try { s = await api.get('/api/status'); }
    catch { return; }
    heroText.className = 'hero-text';
    heroPulse.className = 'pulse';
    if (s.status === 'running' && s.paused) {
      heroText.textContent = 'PAUSED'; heroText.classList.add('paused');
      heroPulse.classList.add('paused');
      pauseBtn.textContent = 'Resume';
    } else if (s.status === 'running') {
      heroText.textContent = 'ONLINE'; heroPulse.classList.add('online');
      pauseBtn.textContent = 'Pause';
    } else {
      heroText.textContent = 'OFFLINE'; heroText.classList.add('offline');
      heroPulse.classList.add('offline');
      pauseBtn.textContent = 'Pause';
    }
    document.getElementById('stat-think').textContent = s.think_calls ?? '0';
    document.getElementById('stat-tin').textContent  = fmtNum(s.tokens_in);
    document.getElementById('stat-tout').textContent = fmtNum(s.tokens_out);
    document.getElementById('stat-uptime').textContent = fmtUptime(s.uptime_seconds);
    document.getElementById('stat-last').textContent = s.last_message_preview || '—';
    document.getElementById('stat-morning').textContent = s.next_morning_briefing || '—';
    document.getElementById('stat-evening').textContent = s.next_evening_briefing || '—';
  }

  tick();
  STATUS_TIMER = setInterval(tick, 5000);

  document.querySelector('[data-action="brief"]').onclick = async () => {
    try { await api.post('/api/friday/brief'); flash('BRIEFING REQUESTED'); }
    catch { flash('FAILED', true); }
  };
  pauseBtn.onclick = async () => {
    let s;
    try { s = await api.get('/api/status'); } catch { return; }
    const next = !s.paused;
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
}

function fmtNum(n) {
  if (n == null) return '—';
  const v = parseInt(n, 10);
  if (isNaN(v)) return '—';
  return v.toLocaleString();
}

function fmtUptime(secs) {
  if (secs == null) return '—';
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
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
      await saveConfig();
    };
    container.appendChild(row);
  }
  togglePhrases();

  const ci = document.getElementById('custom-instructions');
  ci.value = p.custom_instructions || '';
  ci.onblur = async () => {
    p.custom_instructions = ci.value;
    await saveConfig();
  };
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

function renderCalendar() {
  bindInput(document.getElementById('default-calendar'), 'agent.default_calendar');
  renderGcalRows();
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
    row.innerHTML = `
      <input class="input" placeholder="Calendar name" value="${escapeAttr(cal.name || '')}">
      <input class="input" placeholder="https://calendar.google.com/calendar/ical/..." value="${escapeAttr(cal.ical_url || '')}">
      <span class="last-sync"></span>
      <button class="icon-btn" title="Remove">×</button>
    `;
    const [nameEl, urlEl] = row.querySelectorAll('.input');
    nameEl.onblur = async () => { cal.name = nameEl.value; await saveConfig(); };
    urlEl.onblur = async () => { cal.ical_url = urlEl.value; await saveConfig(); };
    row.querySelector('.icon-btn').onclick = async () => {
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
  const initial = (location.hash.replace('#/', '') || 'status');
  navigate(initial);
}

boot();
