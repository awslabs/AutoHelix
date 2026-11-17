// AutoHelix dashboard: the project's runs, from /api/runs. Each row opens /run/<id>/.
// Shared helpers (el, number and time formats, splitRunId, runNotes, themeButton): common.js.
'use strict';

const POLL_MS = 2000;
let data = null;
let metric = null; // the metric column's, from the menu in its header
let showArchived = false;

function fmtAgo(epochSeconds) {
  if (typeof epochSeconds !== 'number') return '';
  const s = Date.now() / 1000 - epochSeconds;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(epochSeconds * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

const live = (run) => run.status?.state === 'running';

// One word for the row's state; archived wins over the last segment's outcome.
function outcome(run) {
  if (run.error) return 'error';
  if (live(run)) return 'running';
  if (run.archived) return 'archived';
  return run.status?.state || 'idle';
}

const WORDS = { finished: 'finished', interrupted: 'interrupted', failed: 'failed', crashed: 'stopped unexpectedly',
                idle: 'not started', archived: 'archived', running: 'running', error: 'unreadable' };

// The iteration under way while running, otherwise the last one finished.
function currentIteration(run) {
  const s = run.status, p = run.progress;
  return live(run) && s.iteration > p.iterations ? s.iteration : p.iterations;
}

// The running segment's --time against its clock (run_table.time_cap): "12m5s of 1h
// (this segment)", short "12m5s/1h"; past the cap "(time up)". '' when the run isn't
// running or the segment has no --time (a stop reason says when time ended it).
function timeCap(run, short = false) {
  const s = run.status, cap = run.progress?.max_seconds;
  if (!cap || !live(run) || typeof s.segment_started_at !== 'number') return '';
  const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - s.segment_started_at));
  const text = short ? `${fmtLimit(elapsed)}/${fmtLimit(cap)}` : `${fmtLimit(elapsed)} of ${fmtLimit(cap)} (this segment)`;
  return elapsed >= cap ? `${text} (time up)` : text;
}

// Line 1: the state, with the phase while running (the console's word for it, the server's
// phase_word: agent, gates, observe · bench, baseline).
// Line 2: time and cost so far, and while running the segment's time against its --time.
// Stop reason, budgets (the latest segment's) and that time in full are in the tooltip.
function statusCell(run) {
  const kind = outcome(run);
  const s = run.status;
  const p = run.progress;
  let detail = '';
  let word = WORDS[kind] || kind;
  const notes = [];
  if (kind === 'running') {
    detail = s.phase_word || s.phase || '';
    notes.push(`${detail || 'running'} for ${fmtDuration(Date.now() / 1000 - s.phase_started_at)}`);
    const asked = run.stop_requested;  // `autohelix stop`: it ends after this iteration
    if (asked) {
      word = 'running (stopping)';
      notes.push(`Stop requested${asked.now ? ' (--now)' : ''}${asked.why ? `: ${asked.why}` : ''}`);
    }
  } else if (kind === 'error') {
    notes.push(run.error);
  } else if (s?.stop_reason?.detail) {
    notes.push(`Stopped: ${s.stop_reason.detail}`);
  }
  let spent = null;
  if (p) {
    // A running iteration's time so far is included; its cost arrives when it ends.
    const seconds = p.seconds + (currentIteration(run) > p.iterations && s.iteration_started_at
      ? Date.now() / 1000 - s.iteration_started_at : 0);
    const cost = p.cost_usd;
    // The run's total time (no cap) and cost, then the running segment's time against
    // its --time (never a run total). --cost is a run total (spent before it + its cap).
    spent = [seconds ? fmtDuration(seconds) : null,
      isNumber(cost) ? `$${cost.toFixed(2)}${p.cost_incomplete ? '+' : ''}` : null,
      timeCap(run, true)].filter(Boolean).join(' · ');
    const cap = timeCap(run);
    if (cap) notes.push(`Time budget: ${cap}`);
    if (p.max_cost_usd) notes.push(`Cost budget ${fmtCap(p.max_cost_usd)} (run total)`);
    if (p.cost_incomplete) notes.push('Some call costs are unavailable');
  }
  return el('td', { title: notes.join('\n') },
    el('div', {}, el('span', { className: `pill ${kind}`, textContent: word }),
      detail ? el('span', { className: 'muted', textContent: ` ${detail}` }) : null),
    spent ? el('div', { className: 'status-sub', textContent: spent }) : null);
}

function iterationsCell(run) {
  const p = run.progress;
  if (!p) return el('td');
  const n = currentIteration(run);
  return el('td', { className: 'counts', title: live(run) && n > p.iterations ? `Iteration ${n} is under way` : '' },
    p.max_iterations ? `${n} / ${p.max_iterations}` : String(n));
}

// Metric names across runs, declared order first; the header menu picks one.
function metricNames(runs) {
  const names = [];
  for (const r of runs) for (const v of r.metrics?.values || []) if (!names.includes(v.name)) names.push(v.name);
  return names;
}

function metricCell(run) {
  const m = run.metrics;
  const v = m?.values.find((x) => x.name === metric);
  if (!v) return el('td', { className: 'value muted', textContent: '—' });
  // Raw values compare across runs; a change vs each run's own baseline would not,
  // so it is only in the tooltip (and on the run page).
  const delta = m.iteration > 0 ? formatDelta(v.value, v.baseline) : '';
  const title = m.iteration === 0 ? 'Baseline (nothing accepted yet)'
    : `Iteration ${m.iteration}, the latest accepted` + (isNumber(v.baseline)
      ? `\nThis run's baseline: ${fmtNumber(v.baseline)}${delta ? ` (${delta})` : ''}` : '');
  return el('td', { className: 'value', title }, fmtNumber(v.value));
}

function landingCell(run) {
  const target = run.target_branch;
  // Words rather than a bare dash: each case says why there is nothing to land.
  if (!target) return el('td', { className: 'muted', textContent: 'detached', title: 'Started on a detached HEAD: land with autohelix land --into <branch>' });
  const l = run.landing;  // runs.landing's relation; `text` says what it means (runs.landing_words)
  if (!l) return el('td', { className: 'muted', textContent: 'unavailable', title: `Branch ${target} or the run's commits are not available` });
  if (l.relation !== 'diverged' && l.commits === 0) return el('td', { className: 'muted', textContent: run.accepted ? 'landed' : 'nothing yet', title: l.text });
  const n = plural(l.commits, 'commit');
  if (l.relation !== 'diverged') return el('td', { className: 'landing', title: l.text }, `${n} to land`);
  return el('td', { className: 'landing', title: l.text }, `${n} · `, el('span', { className: 'warn', textContent: 'diverged' }));
}

function lastActiveCell(run) {
  const when = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  const title = [run.last_activity && `Last active ${when(run.last_activity)}`,
    run.created_at && `Created ${when(run.created_at)}`].filter(Boolean).join('\n');
  return el('td', { className: 'muted wide', title }, live(run) ? 'now' : fmtAgo(run.last_activity) || '—');
}

// The goal's first line (the full goal on hover), and below it why the run was started,
// what it continues from, why it was archived (run.json's notes, as text).
function goalLines(run) {
  const notes = runNotes(run);
  return [run.title ? el('div', { className: 'run-title', textContent: run.title, title: run.goal || run.title }) : null,
    notes ? el('div', { className: 'status-sub run-why', textContent: notes, title: notes }) : null];
}

function runRow(run) {
  const href = `/run/${encodeURIComponent(run.id)}/`;
  const { name, started } = splitRunId(run.id);
  const file = run.config_file && run.config_file
    + (run.config_invalid ? ' (invalid)' : '');
  const tr = el('tr', { className: run.archived ? 'archived' : '', tabIndex: 0 },
    el('td', {}, el('a', { className: 'run-id', href, textContent: name, title: run.id }),
      run.group ? el('span', { className: 'muted', title: 'Group: runs that see each other\'s notes and code' },
        ` (${run.group})`) : null,
      started ? el('span', { className: 'run-date', textContent: started, title: 'Started' }) : null,
      file ? el('div', { className: 'status-sub run-config', textContent: file, title: 'Config file' }) : null,
      // Narrow, the Goal and Last active columns are hidden: the goal goes here.
      el('div', { className: 'narrow' }, ...goalLines(run))),
    statusCell(run),
    iterationsCell(run),
    ...(metric ? [metricCell(run)] : []),
    landingCell(run),
    lastActiveCell(run),
    // Last, as in the terminal table.
    el('td', { className: 'wide' }, ...goalLines(run)));
  tr.addEventListener('click', (e) => { if (!e.target.closest('a')) location.href = href; });
  tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') location.href = href; });
  return tr;
}

function renderMetricMenu(names) {
  if (!names.includes(metric)) metric = names[0] || null;
  $('metric-head').hidden = !metric; // no run has metrics: no column
  fillMenu($('metric'), names, metric);
}

// "5 runs · $2.95 spent · 1 archived (show)": the count toggles archived rows.
function renderSubtitle(runs) {
  const archived = runs.filter((r) => r.archived).length;
  // Total spend includes archived runs: that money was spent.
  const costs = runs.map((r) => r.progress?.cost_usd).filter(isNumber);
  const incomplete = runs.some((r) => r.progress?.cost_incomplete || (r.progress && r.progress.cost_usd === null && r.progress.iterations));
  const parts = [plural(runs.length - archived, 'run')];
  if (costs.length) {
    parts.push(el('span', { title: incomplete ? 'Some call costs are unavailable, so this is a lower bound' : 'All runs, including archived ones',
      textContent: `${fmtMoney(costs.reduce((a, b) => a + b, 0), incomplete)} spent` }));
  }
  if (archived) {
    parts.push(el('button', { className: 'link', textContent: `${archived} archived (${showArchived ? 'hide' : 'show'})`,
      onclick: () => { showArchived = !showArchived; render(); } }));
  }
  $('subtitle').replaceChildren(...parts.flatMap((part, i) => (i ? [' · ', part] : [part])));
}

function render() {
  const runs = data.runs;
  const order = (r) => (live(r) ? 0 : r.archived ? 2 : 1);
  const shown = runs.filter((r) => showArchived || !r.archived)
    .sort((a, b) => order(a) - order(b) || (b.last_activity || 0) - (a.last_activity || 0));
  setText('title', data.project);
  document.title = `${data.project} · AutoHelix dashboard`;
  // Which machine and checkout: over SSH tunnels every dashboard is localhost.
  const head = data.commit && `${data.branch || 'detached'} @ ${data.commit}`;
  setText('where', [[data.host, data.path_display].filter(Boolean).join(':'), head].filter(Boolean).join(' · '));
  $('where').title = [data.path, head && `Checked out: ${head}`].filter(Boolean).join('\n');
  renderSubtitle(runs);
  const running = runs.filter(live).length;
  setText('status', running ? `${running} live` : 'No live runs');
  $('status').className = running ? 'status running' : 'status';
  renderMetricMenu(metricNames(runs));
  $('runs').replaceChildren(...shown.map(runRow));
  $('empty').hidden = runs.length > 0;
}

async function loop() {
  try {
    const res = await fetch('/api/runs', { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
    render();
  } catch (err) {
    console.error(err);
    setText('status', 'Disconnected — retrying');
    $('status').className = 'status error';
  } finally {
    setTimeout(loop, document.hidden ? POLL_MS * 5 : POLL_MS);
  }
}

themeButton();
$('metric').addEventListener('change', (e) => { metric = e.target.value; if (data) render(); });
loop();
