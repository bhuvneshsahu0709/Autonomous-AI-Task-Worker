/* Operator console.
 *
 * Deliberately dependency-free: no build step, no framework, no node_modules.
 * Anyone cloning this repo can run the demo with `python -m app` and nothing
 * else, and the whole UI is one readable file.
 *
 * State comes from a single SSE stream per run. The server replays the full
 * event history on connect, so reloading the page mid-run rebuilds the trace
 * from the beginning rather than starting blank.
 */

const $ = (id) => document.getElementById(id);

const state = {
  runId: null,
  source: null,
  config: null,
  steps: new Map(),
  stats: { steps: 0, errors: 0, retries: 0 },
};

// ---------------------------------------------------------------- utilities
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function clearTimeline() {
  $('timeline').innerHTML = '';
  state.steps.clear();
  state.stats = { steps: 0, errors: 0, retries: 0 };
  renderStats();
}

function scrollTrace() {
  const tl = $('timeline');
  const last = tl.lastElementChild;
  if (last) last.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

function renderStats() {
  const { steps, errors, retries } = state.stats;
  $('trace-stats').innerHTML =
    `<span><b>${steps}</b> actions</span>` +
    (errors ? `<span><b>${errors}</b> failed</span>` : '') +
    (retries ? `<span><b>${retries}</b> auto-retried</span>` : '');
}

// ---------------------------------------------------------------- bootstrap
async function boot() {
  const res = await fetch('/api/config');
  state.config = await res.json();
  const c = state.config;

  $('topbar-meta').innerHTML =
    `<span>model <b>${esc(c.settings.model)}</b></span>` +
    `<span>autonomy <b>${esc(c.settings.autonomy_level)}</b></span>` +
    `<span>approval over <b>$${c.settings.approval_amount_threshold.toLocaleString()}</b></span>` +
    `<a href="/" target="_blank">simulated systems ↗</a>`;

  const box = $('examples');
  c.examples.forEach((ex) => {
    const b = el('button', 'example');
    b.innerHTML = `<b>${esc(ex.title)}</b><span>${esc(ex.note)}</span>`;
    b.onclick = () => { $('goal').value = ex.goal; $('goal').focus(); };
    box.appendChild(b);
  });

  const faults = $('fault-list');
  Object.entries(c.available_faults).forEach(([key, desc]) => {
    const wrap = el('label', 'fault');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.value = key;
    cb.checked = c.active_faults.includes(key);
    cb.dataset.fault = key;
    const txt = el('div');
    txt.innerHTML = `<b>${esc(key)}</b><span>${esc(desc)}</span>`;
    wrap.append(cb, txt);
    faults.appendChild(wrap);
  });

  if (!c.has_api_key) {
    showError('No ANTHROPIC_API_KEY configured — copy .env.example to .env and add a key. ' +
              'You can still run the scripted demo (under Run options) to see the runtime work.');
    $('scripted').checked = true;
    $('panel-task').querySelector('.opts').open = true;
  }

  loadHistory();
}

function showError(msg) {
  const node = $('run-error');
  node.textContent = msg;
  node.hidden = false;
}

async function loadHistory() {
  const res = await fetch('/api/runs?limit=12');
  const { runs } = await res.json();
  const box = $('history');
  box.innerHTML = '';
  if (!runs.length) {
    box.appendChild(el('div', 'empty-inline', 'No runs yet.'));
    return;
  }
  runs.forEach((r) => {
    const row = el('div', 'hist-row');
    row.innerHTML =
      `<span class="st ${r.status}">${r.status}</span>` +
      `<span class="goal">${esc(r.goal)}</span>` +
      `<span class="ms">${r.steps}</span>`;
    row.onclick = () => openRun(r.id);
    box.appendChild(row);
  });
}

// ---------------------------------------------------------------- run control
async function startRun() {
  const goal = $('goal').value.trim();
  if (goal.length < 3) { showError('Describe the task first.'); return; }
  $('run-error').hidden = true;

  const faults = [...document.querySelectorAll('[data-fault]')]
    .filter((c) => c.checked).map((c) => c.value);

  $('run-btn').disabled = true;
  clearTimeline();
  $('panel-result').hidden = true;
  $('panel-facts').hidden = true;
  $('panel-human').hidden = true;

  let res;
  try {
    res = await fetch('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        goal,
        allow_human: $('allow-human').checked,
        reset_sandbox: $('reset-sandbox').checked,
        planner: $('scripted').checked ? 'scripted' : 'llm',
        faults,
      }),
    });
  } catch (e) {
    $('run-btn').disabled = false;
    showError('Could not reach the server.');
    return;
  }

  if (!res.ok) {
    $('run-btn').disabled = false;
    const body = await res.json().catch(() => ({}));
    showError(body.detail || `Could not start the run (HTTP ${res.status}).`);
    return;
  }

  const { run } = await res.json();
  attach(run.id);
  $('cancel-btn').hidden = false;
}

function attach(runId) {
  if (state.source) state.source.close();
  state.runId = runId;
  state.source = new EventSource(`/api/runs/${runId}/events`);
  state.source.onmessage = (e) => handleEvent(JSON.parse(e.data));
  state.source.onerror = () => { /* EventSource retries on its own */ };
}

async function openRun(runId) {
  clearTimeline();
  state.runId = runId;  // thumbnails resolve artifact URLs against this
  if (state.source) { state.source.close(); state.source = null; }
  const res = await fetch(`/api/runs/${runId}`);
  if (!res.ok) return;
  const { run } = await res.json();

  run.steps.forEach((s) => {
    renderStepStart({
      index: s.index, phase: s.phase, tool: s.tool,
      summary: JSON.stringify(s.args).slice(0, 140), thought: s.thought,
    });
    renderStepEnd({
      index: s.index, status: s.status, error_kind: s.error_kind,
      retries: s.retries, duration_ms: s.duration_ms,
      observation: s.observation, artifacts: s.artifacts,
    });
  });
  renderFacts(run.facts);
  renderResult(run);
  $('cancel-btn').hidden = true;
  $('run-btn').disabled = false;
}

async function cancelRun() {
  if (!state.runId) return;
  await fetch(`/api/runs/${state.runId}/cancel`, { method: 'POST' });
}

// ---------------------------------------------------------------- events
function handleEvent(evt) {
  const d = evt.data || {};
  switch (evt.type) {
    case 'step.started':      renderStepStart(d); break;
    case 'step.completed':    renderStepEnd(d); break;
    case 'step.retry':        renderRetry(d); break;
    case 'fact.added':        addFact(d); break;
    case 'log':               renderNote(d.message, d.level); break;
    case 'human.prompt':      renderHumanPrompt(d.prompt); break;
    case 'human.answered':    $('panel-human').hidden = true;
                              renderNote(answerText(d.prompt), 'system'); break;
    case 'verification.started':
      renderNote(`Independent verification started (round ${d.round}). ` +
                 `Re-reading the systems of record to check ${Object.keys(d.claims || {}).length} claim(s).`,
                 'system');
      break;
    case 'verification.completed':
      renderNote(`Verification verdict: ${d.report.verdict.toUpperCase()}.`,
                 d.report.verdict === 'verified' ? 'system' : 'warn');
      break;
    case 'run.finished':      onFinished(d); break;
  }
}

function answerText(p) {
  if (p.kind === 'approval') {
    return `Operator ${p.approved ? 'approved' : 'declined'} the action.` +
           (p.answer ? ` Note: ${p.answer}` : '');
  }
  return `Operator answered: ${p.answer || '(no answer)'}`;
}

// ---------------------------------------------------------------- rendering
function renderStepStart(d) {
  const node = el('div', `step running ${d.phase === 'verify' ? 'verify' : ''}`);
  node.dataset.index = d.index;

  const head = el('div', 'step-head');
  head.append(
    el('span', 'idx', String(d.index)),
    el('span', 'tool', d.tool),
    el('span', 'arg', d.summary || ''),
  );
  if (d.phase === 'verify') head.appendChild(el('span', 'badge phase', 'verify'));
  head.appendChild(el('span', 'ms', '…'));
  head.onclick = () => node.classList.toggle('open');
  node.appendChild(head);

  if (d.thought) node.appendChild(el('div', 'thought', d.thought));
  node.appendChild(el('div', 'step-body'));

  $('timeline').appendChild(node);
  state.steps.set(d.index, node);
  scrollTrace();
}

function renderStepEnd(d) {
  const node = state.steps.get(d.index);
  if (!node) return;

  node.classList.remove('running');
  node.classList.add(d.status === 'ok' ? 'ok' : 'error');
  if (d.tool === 'finish' || d.tool === 'report_verification') node.classList.add('milestone');

  const head = node.querySelector('.step-head');
  head.querySelector('.ms').textContent = `${d.duration_ms} ms`;

  if (d.status !== 'ok') {
    const badge = el('span', 'badge err', d.error_kind || 'error');
    head.insertBefore(badge, head.querySelector('.ms'));
    state.stats.errors += 1;
  }
  if (d.retries) {
    head.insertBefore(el('span', 'badge retry', `${d.retries}× retried`), head.querySelector('.ms'));
    state.stats.retries += d.retries;
  }

  const body = node.querySelector('.step-body');
  body.innerHTML = '';
  if (d.observation) body.appendChild(el('pre', 'obs', d.observation));

  const shots = (d.artifacts || []).filter((a) => a.kind === 'screenshot');
  if (shots.length) {
    const strip = el('div', 'shots');
    shots.forEach((a) => strip.appendChild(thumb(a)));
    body.appendChild(strip);
  }
  // Failures are opened automatically: they are the interesting ones.
  if (d.status !== 'ok') node.classList.add('open');

  state.stats.steps += 1;
  renderStats();
  scrollTrace();
}

function thumb(artifact) {
  const img = el('img');
  img.src = `/api/runs/${state.runId}/artifacts/${artifact.path}`;
  img.alt = artifact.label || '';
  img.title = artifact.label || '';
  img.loading = 'lazy';
  img.onclick = (e) => { e.stopPropagation(); openLightbox(img.src); };
  return img;
}

function renderRetry(d) {
  renderNote(`Retrying \`${d.tool}\` (attempt ${d.attempt}) after a transient failure: ${d.error}`, 'warn');
}

function renderNote(message, level) {
  const node = el('div', `note ${level === 'system' ? 'system' : ''}`);
  node.textContent = message;
  $('timeline').appendChild(node);
  scrollTrace();
}

function addFact(fact) {
  $('panel-facts').hidden = false;
  const table = $('facts');
  const existing = table.querySelector(`[data-key="${CSS.escape(fact.key)}"]`);
  if (existing) existing.remove();

  const row = el('tr');
  row.dataset.key = fact.key;
  row.innerHTML =
    `<td class="k">${esc(fact.key)}</td>` +
    `<td class="v">${esc(fact.value)}<span class="note">${esc(fact.note || '')}</span></td>`;
  table.appendChild(row);
  $('fact-count').textContent = `(${table.children.length})`;
}

function renderFacts(facts) {
  $('facts').innerHTML = '';
  (facts || []).forEach(addFact);
  $('panel-facts').hidden = !(facts || []).length;
}

function renderHumanPrompt(p) {
  const panel = $('panel-human');
  panel.hidden = false;
  $('human-question').textContent = p.question;
  $('human-details').hidden = !p.details;
  $('human-details').textContent = p.details || '';
  $('human-risk').hidden = !p.risk;
  $('human-risk').textContent = p.risk || '';
  $('human-answer').value = '';

  const actions = $('human-actions');
  actions.innerHTML = '';
  if (p.kind === 'approval') {
    const yes = el('button', 'btn ok', 'Approve');
    yes.onclick = () => respond({ approved: true, answer: $('human-answer').value });
    const no = el('button', 'btn danger', 'Decline');
    no.onclick = () => respond({ approved: false, answer: $('human-answer').value });
    actions.append(yes, no);
  } else {
    (p.options || []).forEach((opt) => {
      const b = el('button', 'btn ghost', opt);
      b.onclick = () => respond({ answer: opt });
      actions.appendChild(b);
    });
    const send = el('button', 'btn primary', 'Send answer');
    send.onclick = () => respond({ answer: $('human-answer').value });
    actions.appendChild(send);
  }
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function respond(payload) {
  $('panel-human').hidden = true;
  await fetch(`/api/runs/${state.runId}/respond`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

async function onFinished(d) {
  $('run-btn').disabled = false;
  $('cancel-btn').hidden = true;
  $('panel-human').hidden = true;
  if (state.source) { state.source.close(); state.source = null; }

  const res = await fetch(`/api/runs/${state.runId}`);
  if (res.ok) renderResult((await res.json()).run);
  loadHistory();
}

function renderResult(run) {
  const panel = $('panel-result');
  panel.hidden = false;
  const body = $('result-body');
  body.innerHTML = '';

  const v = run.verification;
  const verdict = run.status === 'succeeded' ? 'verified' : (v ? v.verdict : 'inconclusive');
  const chip = el('span', `verdict ${verdict}`, statusLabel(run.status, v));
  body.appendChild(chip);

  body.appendChild(el('div', 'outcome', run.outcome || run.failure_reason || '—'));

  if (v && v.checks && v.checks.length) {
    const table = el('table', 'claims');
    table.innerHTML =
      '<tr><th></th><th>claim</th><th>agent said</th><th>system shows</th></tr>' +
      v.checks.map((c) =>
        `<tr><td class="mark">${c.ok ? '✓' : '✕'}</td>` +
        `<td>${esc(c.key)}</td><td>${esc(c.claimed)}</td><td>${esc(c.observed)}</td></tr>`
      ).join('');
    body.appendChild(table);
  }
  if (v && v.reasoning) {
    body.appendChild(el('div', 'reasoning', v.reasoning));
  }

  const shots = (run.artifacts || []).filter((a) => a.kind === 'screenshot').slice(-8);
  if (shots.length) {
    const strip = el('div', 'evidence');
    shots.forEach((a) => {
      const img = el('img');
      img.src = `/api/runs/${run.id}/artifacts/${a.path}`;
      img.title = a.label;
      img.loading = 'lazy';
      img.onclick = () => openLightbox(img.src);
      strip.appendChild(img);
    });
    body.appendChild(strip);
  }

  const u = run.usage || {};
  body.appendChild(el('div', 'reasoning',
    `${run.steps.length} actions · ${u.llm_calls || 0} model calls · ` +
    `${(u.input_tokens || 0).toLocaleString()} in / ${(u.output_tokens || 0).toLocaleString()} out tokens` +
    (u.cache_read_tokens ? ` · ${u.cache_read_tokens.toLocaleString()} cached` : '')));
}

function statusLabel(status, v) {
  if (status === 'succeeded') return 'verified complete';
  if (status === 'awaiting_human') return 'waiting for you';
  if (status === 'cancelled') return 'cancelled';
  if (v && v.verdict === 'refuted') return 'not verified';
  return status;
}

function openLightbox(src) {
  $('lightbox-img').src = src;
  $('lightbox').hidden = false;
}

// ---------------------------------------------------------------- wiring
$('run-btn').onclick = startRun;
$('cancel-btn').onclick = cancelRun;
$('lightbox').onclick = () => { $('lightbox').hidden = true; };
$('goal').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) startRun();
});

boot();
