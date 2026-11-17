// AutoHelix run list: the project's runs, from /api/runs. Each row opens /run/<id>/.
// Formatting: common.js. The rows say what the console's dashboard says.
'use strict';

const POLL_MS = 2000;
let data = null;
let metric = null; // the metric column's, from the menu in its header
let showArchived = false;

const live = (run) => run.status?.state === 'running';

// The console dashboard's cells (the server's `words`): line 1 the state and what a
// running run is doing (`agent 1m 12s`; narrow, also when a stopped one was last active,
// whose column is gone there); line 2 time (baseline included) and cost so far, and
// what is left of --time. Stop request, stop reason, budgets and dates are in the tooltip.
function statusCell(run) {
  const w = run.words;
  const s = run.status;
  const p = run.progress;
  const asked = run.stop_requested; // `autohelix stop`: it ends after this iteration ("running (stopping)")
  const notes = [
    run.error,
    asked && `Stop requested${asked.now ? ' (--now)' : ''}${asked.why ? `: ${asked.why}` : ''}`,
    !live(run) && s?.stop_reason?.detail && `Stopped: ${s.stop_reason.detail}`,
    run.last_activity && `Last active ${fmtClock(run.last_activity)}`,
    run.created_at && `Created ${fmtClock(run.created_at)}`,
    p?.max_cost_usd && `Cost budget ${fmtCap(p.max_cost_usd)} (run total)`,
    p?.cost_incomplete && 'Some call costs are unknown',
  ];
  const spent = [w.spent, w.time_left].filter(Boolean).join(' · ');
  return el('td', { title: notes.filter(Boolean).join('\n') },
    el('div', {}, el('span', { className: `state ${w.tone || ''}`, textContent: w.state }),
      w.activity ? el('span', { className: live(run) ? 'muted' : 'muted narrow', textContent: ` ${w.activity}` }) : null),
    spent ? el('div', { className: 'sub', textContent: spent }) : null);
}

function iterationsCell(run) {
  return el('td', { className: 'num', textContent: run.words.iterations || '' });
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

// `landed`, `3 to land`, `3 diverged` (grey: normal for a manual run once the branch
// moved on), what to type in the tooltip (common.js landingWords).
function landingCell(run) {
  const { text, hint, todo } = landingWords(run, live(run));
  return el('td', { className: todo ? '' : 'muted', textContent: text, title: hint });
}

// The run: its name and start, the group, the config file under them. Narrow, the
// goal's cell goes under them too (goalLines).
function runCell(run, href) {
  const { name, started } = splitRunId(run.id);
  const file = run.config_file && run.config_file
    + (run.config_missing ? ' (not stored)' : run.config_invalid ? ' (invalid)' : '');
  return el('td', {},
    el('div', {}, el('a', { className: 'run-id', href, textContent: name, title: run.id }),
      run.group ? el('span', { className: 'muted', title: 'Group: runs that see each other\'s notes and code', textContent: ` (${run.group})` }) : null,
      started ? el('span', { className: 'run-date', textContent: started, title: 'Started' }) : null),
    file ? el('div', { className: 'sub run-config', textContent: file, title: 'Config file' }) : null,
    el('div', { className: 'narrow' }, ...goalLines(run)));
}

// The goal's first line (the whole goal on hover) and run.json's notes under it (why,
// what it continues from, why archived), always as text.
function goalLines(run) {
  const notes = runNotes(run);
  return [run.title ? el('div', { className: 'run-title', textContent: run.title, title: run.goal || run.title }) : null,
    notes ? el('div', { className: 'sub', textContent: notes, title: notes }) : null];
}

function runRow(run) {
  const href = `/run/${encodeURIComponent(run.id)}/`;
  const tr = el('tr', { className: run.archived ? 'archived' : '', tabIndex: 0 },
    runCell(run, href), statusCell(run), iterationsCell(run), metric ? metricCell(run) : null, landingCell(run),
    el('td', { className: 'wide muted', textContent: live(run) ? 'now' : run.words.activity || '' }),
    el('td', { className: 'wide goal' }, ...goalLines(run)));
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
