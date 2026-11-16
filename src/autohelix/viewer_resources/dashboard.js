// AutoHelix run list: the project's runs, from /api/runs. Each row opens /run/<id>/.
// Formatting: common.js. The rows say what the console's dashboard says.
'use strict';

const POLL_MS = 2000;
let data = null;
let metric = null; // the metric column's, from the menu in its header
let showArchived = false;

const live = (run) => run.status?.state === 'running';
const shortId = (run) => run.id.split('-').at(-1);

function fmtAgo(epochSeconds) {
  if (typeof epochSeconds !== 'number') return '';
  const s = Date.now() / 1000 - epochSeconds;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(epochSeconds * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

// One word for the row's state; archived wins over the last segment's outcome.
function outcome(run) {
  if (run.error) return 'error';
  if (live(run)) return 'running';
  if (run.archived) return 'archived';
  return run.status?.state || 'idle';
}

const WORDS = { crashed: 'stopped unexpectedly', idle: 'not started', error: 'unreadable' };

// The iteration under way while running, otherwise the last one finished.
function currentIteration(run) {
  const s = run.status, p = run.progress;
  return live(run) && s.iteration > p.iterations ? s.iteration : p.iterations;
}

// The running segment's --time against its clock: "12m5s of 1h (this segment)", short
// "12m5s/1h"; past the cap "(time up)". '' when the run isn't running or the segment
// has no --time (a stop reason says when time ended it).
function timeCap(run, short = false) {
  const s = run.status, cap = run.progress?.max_seconds;
  if (!cap || !live(run) || typeof s.segment_started_at !== 'number') return '';
  const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - s.segment_started_at));
  const text = short ? `${fmtLimit(elapsed)}/${fmtLimit(cap)}` : `${fmtLimit(elapsed)} of ${fmtLimit(cap)} (this segment)`;
  return elapsed >= cap ? `${text} (time up)` : text;
}

// Line 1: the state and what a running run is doing (`agent 1m 12s`), else when it was
// last active. Line 2: time (baseline included) and cost so far, and the --time left.
// Stop reason, budgets and dates are in the tooltip.
function statusCell(run) {
  const kind = outcome(run);
  const s = run.status;
  const p = run.progress;
  const notes = [];
  let word = WORDS[kind] || kind;
  let activity = '';
  if (kind === 'running') {
    activity = `${phaseWord(s)} ${fmtDuration(Date.now() / 1000 - s.phase_started_at)}`;
    const asked = run.stop_requested; // `autohelix stop`: it ends after this iteration
    if (asked) {
      word = 'running (stopping)';
      notes.push(`Stop requested${asked.now ? ' (--now)' : ''}${asked.why ? `: ${asked.why}` : ''}`);
    }
  } else if (kind === 'error') {
    notes.push(run.error);
  } else {
    activity = fmtAgo(run.last_activity);
    if (s?.stop_reason?.detail) notes.push(`Stopped: ${s.stop_reason.detail}`);
  }
  const when = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  if (run.last_activity) notes.push(`Last active ${when(run.last_activity)}`);
  if (run.created_at) notes.push(`Created ${when(run.created_at)}`);
  let spent = '';
  if (p) {
    // A running iteration's time so far is included; its cost arrives when it ends.
    const seconds = p.seconds + (currentIteration(run) > p.iterations && s.iteration_started_at
      ? Date.now() / 1000 - s.iteration_started_at : 0);
    spent = [fmtDuration(seconds), fmtMoney(p.cost_usd, p.cost_incomplete), timeCap(run, true)].filter(Boolean).join(' · ');
    const cap = timeCap(run);
    if (cap) notes.push(`Time budget: ${cap}`);
    if (p.max_cost_usd) notes.push(`Cost budget ${fmtCap(p.max_cost_usd)} (run total)`);
    if (p.cost_incomplete) notes.push('Some call costs are unknown');
  }
  return el('td', { title: notes.join('\n') },
    el('div', {}, el('span', { className: `state ${kind}`, textContent: word }),
      activity ? el('span', { className: 'muted', textContent: ` ${activity}` }) : null),
    spent ? el('div', { className: 'sub', textContent: spent }) : null);
}

function iterationsCell(run) {
  const p = run.progress;
  if (!p) return el('td');
  const n = currentIteration(run);
  return el('td', { className: 'num', title: live(run) && n > p.iterations ? `Iteration ${n} is under way` : '' },
    p.max_iterations ? `${n}/${p.max_iterations}` : String(n));
}

function metricNames(runs) {
  const names = [];
  for (const r of runs) for (const v of r.metrics?.values || []) if (!names.includes(v.name)) names.push(v.name);
  return names;
}

// The latest accepted value (the code a land would deliver). Raw values compare across
// runs; a change against each run's own baseline would not, so it is in the tooltip.
function metricCell(run) {
  const m = run.metrics;
  const v = m?.values.find((x) => x.name === metric);
  if (!v) return el('td', { className: 'num muted', textContent: '—' });
  const change = m.iteration > 0 ? fmtChange(v.value, v.baseline) : '';
  const title = m.iteration === 0 ? 'Baseline (nothing accepted yet)'
    : `Iteration ${m.iteration}, the latest accepted` + (isNumber(v.baseline)
      ? `\nThis run's baseline: ${fmtNumber(v.baseline)}${change ? ` (${change})` : ''}` : '');
  return el('td', { className: 'num', title }, fmtNumber(v.value));
}

// As the console: `landed`, `3 to land`, `3 diverged` (grey: normal for a manual run once
// the branch moved on), with the mode when it lands by hand; what to type in the tooltip.
function landingCell(run) {
  const target = run.target_branch;
  const manual = run.land === 'manual' ? 'manual · ' : '';
  if (!target) return el('td', { className: 'muted', textContent: 'detached', title: `Started on a detached HEAD: autohelix land ${shortId(run)} --into <branch>` });
  const l = run.landing;
  if (!l) return el('td', { className: 'muted', textContent: '—', title: `Branch ${target} or the run's commits are not available` });
  if (l.commits === 0) {
    return el('td', { className: 'muted', textContent: run.accepted ? 'landed' : '—',
      title: run.accepted ? `Everything accepted is on ${target}` : 'Nothing accepted yet' });
  }
  const n = plural(l.commits, 'commit');
  if (l.fast_forward) {
    return el('td', { textContent: `${manual}${l.commits} to land`,
      title: `${n} not on ${target} yet: autohelix land ${shortId(run)}` });
  }
  return el('td', { className: 'muted', textContent: `${manual}${l.commits} diverged`,
    title: `${target} has commits this run lacks, so it can't fast-forward. Merge by hand: git merge autohelix/${run.id}` });
}

// The run: its name and start, the group and config, then the goal's first line and
// run.json's notes (why, what it continues from, why archived), always as text.
function runCell(run, href) {
  const { name, started } = splitRunId(run.id);
  const file = run.config_file && run.config_file
    + (run.config_missing ? ' (not stored)' : run.config_invalid ? ' (invalid)' : '');
  const notes = runNotes(run);
  return el('td', { className: 'run' },
    el('div', {}, el('a', { className: 'run-id', href, textContent: name, title: run.id }),
      run.group ? el('span', { className: 'muted', title: 'Group: runs that see each other\'s notes and code', textContent: ` (${run.group})` }) : null,
      started ? el('span', { className: 'run-date', textContent: started, title: 'Started' }) : null),
    run.title ? el('div', { className: 'run-title', textContent: run.title, title: run.goal || run.title }) : null,
    file || notes ? el('div', { className: 'sub', textContent: [file, notes].filter(Boolean).join(' · '), title: notes }) : null);
}

function runRow(run) {
  const href = `/run/${encodeURIComponent(run.id)}/`;
  const tr = el('tr', { className: run.archived ? 'archived' : '', tabIndex: 0 },
    runCell(run, href), statusCell(run), iterationsCell(run), metric ? metricCell(run) : null, landingCell(run));
  tr.addEventListener('click', (e) => { if (!e.target.closest('a')) location.href = href; });
  tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') location.href = href; });
  return tr;
}

function renderMetricMenu(names) {
  if (!names.includes(metric)) metric = names[0] || null;
  $('metric-head').hidden = !metric; // no run has metrics: no column
  const menu = $('metric');
  if (JSON.stringify(Array.from(menu.options, (o) => o.value)) !== JSON.stringify(names)) {
    menu.replaceChildren(...names.map((n) => el('option', { value: n, textContent: n })));
  }
  menu.value = metric;
}

// "5 runs · $2.95 spent · 1 archived (show)": the count toggles archived rows.
function renderSubtitle(runs) {
  const archived = runs.filter((r) => r.archived).length;
  // Total spend includes archived runs: that money was spent.
  const costs = runs.map((r) => r.progress?.cost_usd).filter(isNumber);
  const incomplete = runs.some((r) => r.progress?.cost_incomplete || (r.progress && r.progress.cost_usd === null && r.progress.iterations));
  const parts = [plural(runs.length - archived, 'run')];
  if (costs.length) {
    parts.push(el('span', { title: incomplete ? 'Some call costs are unknown, so this is a lower bound' : 'All runs, including archived ones',
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
  $('status').className = `status ${running ? 'running' : 'idle'}`;
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
    $('status').className = 'status crashed';
  } finally {
    setTimeout(loop, document.hidden ? POLL_MS * 5 : POLL_MS);
  }
}

followTheme();
$('metric').addEventListener('change', (e) => { metric = e.target.value; if (data) render(); });
loop();
