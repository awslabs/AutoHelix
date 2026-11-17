// AutoHelix run page: one run's .autohelix/ state. Polls /api/state, and the selected
// iteration's details for the panel beside the table. In report.html the same routes
// come from data built into the file, and nothing is polled. Formatting: common.js.
'use strict';

const POLL_MS = 2000;
const SNAPSHOT = window.AUTOHELIX_SNAPSHOT || null;
// On /run/<id>/ the routes are /api/run/<id>/...; the snapshot keys them as /api/....
const RUN_PAGE = SNAPSHOT ? null : location.pathname.match(/^\/run\/([a-z0-9][a-z0-9-]*)\/$/);
const api = (route) => (RUN_PAGE ? route.replace(/^\/api\//, `/api/run/${RUN_PAGE[1]}/`) : route);
const NARROW = matchMedia('(max-width: 1100px)'); // one column: the panel follows the table

let state = null;
let selected = null;   // the iteration in the panel: always one once the state is in
let followLive = true; // keep selecting the running (or latest) iteration until the user picks another
let metric = null;     // the chart's
const opened = new Map(); // section -> open, as the user left it (kept across selections)

const label = (n) => (n === 0 ? 'Baseline' : `Iteration ${n}`);
const row = (n) => state.iterations.find((r) => r.iteration === n);
const baselineValue = (name) => row(0)?.metrics?.[name];
const valueText = (v) => (isNumber(v) ? fmtNumber(v) : '');
// Why an iteration was rejected, in the console's words (the server's), else its reason.
const whyOf = (r) => (r?.accepted === false && r.iteration > 0 ? r.why || { text: r.reason || 'rejected' } : null);

function show(id, visible) { (typeof id === 'string' ? $(id) : id).hidden = !visible; }

// Only for HTML the server rendered from Markdown with raw HTML disabled.
function setHTML(node, html) {
  if (node.dataset.html !== html) { node.dataset.html = html; node.innerHTML = html; }
}

function changeEl(value, reference, direction) {
  const text = fmtChange(value, reference);
  if (!text) return null;
  const cls = !direction ? '' : better(value, reference, direction) ? 'better' : 'worse';
  return el('span', { className: `change ${cls}`, textContent: text });
}

// Replace a node's children only when `key` (what they are made from) changed: hover
// titles, focus and open folds survive the polls that change nothing.
function rebuild(node, key, build) {
  if (node.dataset.key === key) return false;
  node.dataset.key = key;
  node.replaceChildren(...build());
  return true;
}

// ---------- status ----------

function liveIteration() {
  const s = state?.status;
  return s?.state === 'running' && s.phase !== 'report' && s.iteration !== null ? s.iteration : null;
}

const since = (t) => (typeof t === 'number' ? fmtDuration(Date.now() / 1000 - t) : '');

// Running: `view`'s status line (the server's: `Running · iteration 3 · gates · 12s`);
// else the console end block's first line (the server's `outcome`: Done / Stopped /
// Interrupted / Error, and why), red or yellow where the console's is.
function renderStatus() {
  const s = state.status;
  const node = $('status');
  if (!s) { node.className = 'status'; node.textContent = 'No run recorded'; return; }
  if (s.state === 'running') { node.className = 'status running'; node.textContent = s.line; return; }
  const o = s.outcome || {};
  node.className = `status ${o.tone || ''}`;
  node.textContent = [o.word, o.detail, s.stopped_at && fmtClock(s.stopped_at)].filter(Boolean).join(' · ');
}

// ---------- report, shared notes ----------

function renderReport() {
  const r = state.report;
  show('report', Boolean(r));
  if (!r) return;
  const body = $('report-body');
  if (body.dataset.html !== r.html) {
    setHTML(body, r.html);
    linkIterations(body);
  }
  setText('report-meta', [
    r.last_iteration && `iterations 1–${r.last_iteration}`,
    r.written_at && `written ${fmtClock(r.written_at)}`,
  ].filter(Boolean).join(' · '));
}

// "iter N" in the report selects that iteration.
function linkIterations(root) {
  const known = new Set(state.iterations.map((r) => r.iteration));
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const texts = [];
  while (walker.nextNode()) {
    if (!walker.currentNode.parentElement.closest('a, code, pre')) texts.push(walker.currentNode);
  }
  for (const text of texts) {
    const parts = text.textContent.split(/\b(iter \d+)\b/i);
    if (parts.length === 1) continue;
    text.replaceWith(...parts.map((part) => {
      const n = Number(/^iter (\d+)$/i.exec(part)?.[1]);
      if (!known.has(n)) return part;
      return el('a', { href: `#iter-${n}`, textContent: part, onclick: (e) => { e.preventDefault(); select(n); } });
    }));
  }
}

function renderSharedNotes() {
  // Notes that belong to the whole run rather than one iteration (e.g. ideas.md).
  const files = (state.notes_files || []).filter((name) => !/^iter-\d+\.md$/.test(name));
  show('shared-notes', files.length > 0);
  setText('shared-count', files.join(' · '));
  rebuild($('shared-list'), JSON.stringify(files), () => files.map((name) => {
    const body = el('div', { className: 'prose' });
    const item = el('details', { className: 'note' }, el('summary', { textContent: name }), body);
    item.addEventListener('toggle', async () => {
      if (!item.open) return;
      try { setHTML(body, (await getJSON(`/api/notes/${encodeURIComponent(name)}`)).html || ''); }
      catch { body.textContent = 'Could not load this note.'; }
    });
    return item;
  }));
}

// ---------- stats ----------

// As the console's end block: each metric's tip (the server's: the latest accepted
// value, as the run list shows it) and its change against the baseline, then iterations,
// time (what is left of --time under it) and cost, all the server's (the baseline's time
// included, a running iteration's so far), landing.
function renderStats() {
  const run = state.run;
  const cost = state.cost;
  const stats = [];
  for (const name of metricNames()) {
    const base = baselineValue(name);
    const tip = state.tip?.[name];
    if (!tip) continue;
    const moved = tip.iteration > 0 && tip.value !== base;
    stats.push([name, fmtNumber(tip.value), moved ? [tip.value, base, state.directions[name]] : null,
      !moved ? 'baseline' : isNumber(base) ? `from ${fmtNumber(base)} · iter ${tip.iteration}` : `iter ${tip.iteration}`]);
  }
  if (run) {
    stats.push(['Iterations', run.iterations, null, `${run.accepted} accepted`]);
    stats.push(['Time', fmtDuration(run.seconds) || '—', null, run.time_left]);
    stats.push(['Cost', fmtMoney(cost.cost_usd, cost.incomplete) || '—', null, cost.incomplete ? 'some call costs unknown' : '']);
    const landing = landingWords(run, state.status?.state === 'running');
    if (landing.text !== '—') stats.push(['Landing', landing.text, null, landing.hint]);
  }
  rebuild($('stats'), JSON.stringify(stats), () => stats.map(([k, value, change, sub]) => el('div', { className: 'stat' },
    el('span', { className: 'stat-label', textContent: k }),
    el('span', { className: 'stat-value' }, value, change && changeEl(...change)),
    sub ? el('span', { className: 'stat-sub', textContent: sub }) : null)));
}

function metricNames() {
  const names = new Set(Object.keys(state.directions || {}));
  for (const r of state.iterations) Object.keys(r.metrics || {}).forEach((k) => names.add(k));
  return [...names];
}

// ---------- chart ----------

function svgEl(tag, attrs, text) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

// Every measured value as a dot (hollow: rejected), the tip as a step line (moved by the
// rows that set it, `r.tip`) carried on to the running iteration (a ring at its end).
// Click a dot to select it.
function renderChart() {
  const svg = $('chart');
  const points = metric ? state.iterations.filter((r) => isNumber(r.metrics?.[metric])) : [];
  show('chart-box', points.length > 0); // `hidden` does not apply to <svg> itself
  if (!points.length) return;
  const W = 640, H = 200, pad = { l: 52, r: 12, t: 10, b: 22 };
  const values = points.map((r) => r.metrics[metric]);
  const live = liveIteration();
  const last = state.iterations.at(-1).iteration;
  const n = Math.max(1, last, live ?? 0);
  const lo = Math.min(...values), hi = Math.max(...values);
  const log = lo > 0 && hi / lo > 20; // big speedups are unreadable on a linear axis
  const { ticks, decimals } = log ? logTicks(lo, hi) : linearTicks(lo, hi);
  const f = (v) => (log ? Math.log10(v) : v);
  const [y0, y1] = [f(ticks[0]), f(ticks.at(-1))];
  const x = (i) => pad.l + (i / n) * (W - pad.l - pad.r);
  const y = (v) => (y0 === y1 ? H / 2 : pad.t + (1 - (f(v) - y0) / (y1 - y0)) * (H - pad.t - pad.b));
  const children = [];
  for (const v of ticks) {
    children.push(svgEl('line', { x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v), class: 'grid' }));
    children.push(svgEl('text', { x: pad.l - 6, y: y(v) + 4, 'text-anchor': 'end', class: 'axis' },
      fmtTick(v, decimals)));
  }
  const dir = state.directions[metric];
  setText('scale', [dir && `${dir} is better`, log && 'log scale'].filter(Boolean).join(' · '));
  // Built every poll (cheap); put in the page only when it changed (dot tooltips stay put).
  const step = Math.ceil(n / 10); // whole iterations, evenly spaced
  for (let i = 0; i <= n; i += step) {
    children.push(svgEl('text', { x: x(i), y: H - 4, 'text-anchor': 'middle', class: 'axis' }, String(i)));
  }
  let d = '', tip = null;
  for (const r of points) {
    if (!r.tip) continue;
    tip = r.metrics[metric];
    d += d ? ` H${x(r.iteration)} V${y(tip)}` : `M${x(r.iteration)} ${y(tip)}`;
  }
  if (d) d += ` H${x(live !== null ? n : last)}`;
  children.push(svgEl('path', { d, class: 'tip-line' }));
  const dot = (i, v, cls, title) => {
    const node = svgEl('circle', { cx: x(i), cy: y(v), r: i === selected ? 6 : 4, class: `dot ${cls}${i === selected ? ' selected' : ''}` });
    node.append(svgEl('title', {}, title));
    node.addEventListener('click', () => select(i));
    children.push(node);
  };
  if (live !== null && live > last && tip !== null) dot(live, tip, 'live', `${label(live)}: running`);
  for (const r of points) {
    dot(r.iteration, r.metrics[metric], r.accepted ? 'accepted' : 'rejected', `${label(r.iteration)}: ${fmtNumber(r.metrics[metric])}`);
  }
  rebuild(svg, JSON.stringify([metric, points.map((r) => [r.iteration, r.metrics[metric], r.accepted, r.tip]), live, last, selected]),
    () => children);
}

// Axis ticks: powers of ten on a log axis (written 10, 1K, 10K), round steps on a
// linear one, with as many decimals as the step has (0.25 → 2), so close ticks stay distinct.
function logTicks(lo, hi) {
  const ticks = [];
  for (let k = Math.floor(Math.log10(lo)); k <= Math.ceil(Math.log10(hi)); k++) ticks.push(10 ** k);
  return { ticks, decimals: null };
}

function linearTicks(lo, hi) {
  if (lo === hi) return { ticks: [lo], decimals: null };
  const raw = (hi - lo) / 4;
  const power = Math.floor(Math.log10(raw));
  const m = [1, 2, 2.5, 5, 10].find((k) => k * 10 ** power >= raw);
  const step = m * 10 ** power;
  const ticks = [];
  for (let k = Math.floor(lo / step); k * step <= hi + step * 1e-9; k++) ticks.push(k * step);
  if (ticks.at(-1) < hi) ticks.push(ticks.at(-1) + step);
  return { ticks, decimals: Math.max(0, -power + (m === 2.5 ? 1 : 0) - (m === 10 ? 1 : 0)) };
}

const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumSignificantDigits: 3 });
function fmtTick(v, decimals) {
  if (decimals === null) return compact.format(v);
  return v.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

// ---------- table ----------

// As the console's rows: # and outcome, one column per metric (rejected rows dimmed,
// no change), why it was rejected (grey) or the agent's summary on one line, time, cost.
// Rebuilt only when the rows change (a focused row keeps its focus across polls).
function renderRows() {
  const names = metricNames();
  const arrow = (n) => ({ higher: ' ↑', lower: ' ↓' }[state.directions[n]] || '');
  const head = ['#', '', ...names.map((n) => n + arrow(n)), 'Summary', 'Time', 'Cost'];
  rebuild($('columns'), head.join('\n'), () => head.map((h, i) => el('th', {
    textContent: h, className: i >= 2 && i < 2 + names.length ? 'value' : i > 2 + names.length ? 'num' : '' })));
  const live = liveIteration();
  const running = live !== null && !row(live) ? live : null; // the row under way, not recorded yet
  const box = $('rows');
  rebuild(box, JSON.stringify([head, state.iterations, running, running !== null && state.status.phase_word]), () => {
    const rows = state.iterations.map((r) => {
      const outcome = r.iteration === 0 ? 'baseline' : r.accepted ? 'accepted' : 'rejected';
      const why = whyOf(r);
      const summary = why ? why.text : (r.summary || '').split('\n')[0];
      const notes = (r.notes || []).map((n) => el('span', { className: n.error ? 'flag error' : 'flag', textContent: ` · ${n.text}` }));
      return rowEl(r.iteration, outcome, [
        el('td', { textContent: String(r.iteration) }),
        el('td', { className: `mark ${outcome}`, textContent: { baseline: 'base', accepted: '✓', rejected: '✗' }[outcome] }),
        ...names.map((name) => el('td', { className: `value${outcome === 'rejected' ? ' dim' : ''}` },
          valueText(r.metrics?.[name]),
          outcome === 'accepted' ? changeEl(r.metrics?.[name], baselineValue(name), state.directions[name]) : null)),
        el('td', { className: `summary${why ? ' why' : ''}${why?.error ? ' error' : ''}`, title: [summary, ...(r.notes || []).map((n) => n.text)].join('\n') },
          summary, ...notes),
        el('td', { className: 'num', textContent: fmtDuration(r.seconds) }),
        el('td', { className: 'num', textContent: fmtMoney(r.cost, r.cost_incomplete) || '—' }),
      ]);
    });
    if (running !== null) {
      rows.push(rowEl(running, 'running', [
        el('td', { textContent: String(running) }), el('td', { className: 'mark running', textContent: '●' }),
        ...names.map(() => el('td')),
        el('td', { className: 'summary live', textContent: state.status.phase_word }), // `observe · bench`
        el('td', { className: 'num', id: 'live-time' }), el('td'),
      ]));
    }
    return rows;
  });
  if (running !== null) setText('live-time', since(state.status.iteration_started_at));
  for (const tr of box.children) tr.classList.toggle('selected', Number(tr.dataset.iteration) === selected);
}

function rowEl(iteration, outcome, cells) {
  const tr = el('tr', { tabIndex: 0, title: outcome }, ...cells);
  tr.dataset.iteration = iteration;
  tr.addEventListener('click', () => select(iteration, true));
  tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') select(iteration, true); });
  return tr;
}

// ---------- selection ----------

function iterationOrder() {
  const order = state.iterations.map((r) => r.iteration);
  const live = liveIteration();
  if (live !== null && !order.includes(live)) order.push(live);
  return order;
}

// `byUser` (a row, a dot, j/k): on the one-column layout, bring the panel into view.
function select(iteration, byUser = true) {
  if (iteration === undefined || iteration === null || !state) return;
  followLive = iteration === (liveIteration() ?? state.iterations.at(-1)?.iteration);
  if (iteration !== selected) {
    selected = iteration;
    panel = newPanel(iteration);
  }
  if (byUser) history.replaceState(null, '', followLive ? location.pathname : `#iter-${iteration}`);
  render();
  if (byUser && NARROW.matches) $('panel').scrollIntoView({ block: 'start', behavior: 'smooth' });
  loadDetail();
}

function step(by) {
  const order = iterationOrder();
  const i = order.indexOf(selected);
  select(order[Math.min(order.length - 1, Math.max(0, i + by))]);
}

// ---------- the detail panel ----------
// One iteration: a header (outcome and why), its facts and summary, then folding
// sections: Gates, Changes, one per observe entry, Stop, Agent note, Activity.
// Defaults: Gates open on a rejection by a gate, Changes on an acceptance or while it
// runs; a section the user opened or closed stays so for the next iteration.

let panel = newPanel(null);

function newPanel(iteration) {
  return { iteration, key: undefined, detail: null, sections: new Map(), files: new Map(), diffOpen: new Map(),
           expanded: new Map(), diff: null };
}

function section(key, title, defaultOpen) {
  let s = panel.sections.get(key);
  if (!s) {
    const count = el('span', { className: 'muted' });
    const summary = el('summary', {}, el('b', { textContent: title }), ' ', count);
    const node = el('details', { className: 'sec', open: opened.has(key) ? opened.get(key) : defaultOpen },
      summary, el('div', { className: 'sec-body' }));
    summary.addEventListener('click', () => opened.set(key, !node.open)); // before it toggles
    s = { node, count, body: node.lastChild, key: undefined };
    panel.sections.set(key, s);
  }
  return s;
}

// Fill a section's body only when its data changed (a poll redraws nothing else).
function fill(s, count, key, build) {
  setText(s.count, count ? `· ${count}` : '');
  if (s.key === key) return;
  s.key = key;
  s.body.replaceChildren(...build());
}

async function loadDetail() {
  const n = selected;
  if (n === null || !state) return;
  // Finished iterations change only when their record or notes do; the running one constantly.
  const r = row(n);
  const key = n === liveIteration() && !r ? null : JSON.stringify([r, state.notes_files.includes(`iter-${n}.md`)]);
  if (key !== null && key === panel.key) return;
  let d;
  try { d = await getJSON(`/api/iteration/${n}`); }
  catch { return; } // the next poll retries
  if (selected !== n) return;
  panel.key = key;
  panel.detail = d;
  renderPanel();
  if (d.attempt) loadAttempt(n, d.attempt);
}

// The attempt's own change (start commit to frozen tree), once per attempt.
async function loadAttempt(n, attempt) {
  const key = `${attempt.base}:${attempt.tree}`;
  if (panel.diff?.key === key) return;
  panel.diff = { key, text: null, error: null, loading: true };
  let d = null;
  try { d = await getJSON(`/api/iteration/${n}/diff`); } catch { /* below */ }
  if (selected !== n || panel.diff?.key !== key) return;
  // Not loaded: say so, and try again on the next poll (report.html has no other diff to load).
  panel.diff = d ? { key, text: d.diff, error: d.error }
    : { key: SNAPSHOT ? key : null, text: null, error: SNAPSHOT ? null : 'Could not load the diff.' };
  renderPanel();
}

function renderPanel() {
  const n = selected;
  show('panel', n !== null);
  if (n === null) return;
  const r = row(n);
  const live = !r && n === liveIteration();
  const outcome = live ? 'running' : !r ? '' : n === 0 ? 'baseline' : r.accepted ? 'accepted' : 'rejected';
  const why = whyOf(r);
  $('panel-title').replaceChildren(label(n), ...[
    outcome === 'accepted' && el('span', { className: 'mark accepted', textContent: ' ✓ accepted' }),
    outcome === 'rejected' && el('span', { className: 'mark rejected', textContent: ' ✗ rejected' }),
    outcome === 'running' && el('span', { className: 'mark running', textContent: ' ● running' }), // what: its row, the status line
    why && el('span', { className: `why${why.error ? ' error' : ''}`, textContent: ` · ${why.text}` }),
  ].filter(Boolean));
  const order = iterationOrder();
  $('prev').disabled = order.indexOf(n) <= 0;
  $('next').disabled = order.indexOf(n) >= order.length - 1;
  setText('panel-facts', r ? [fmtDuration(r.seconds), fmtMoney(r.cost, r.cost_incomplete),
    ...metricNames().filter((m) => isNumber(r.metrics?.[m])).map((m) => {
      const change = r.accepted && n > 0 ? fmtChange(r.metrics[m], baselineValue(m)) : '';
      return `${m} ${fmtNumber(r.metrics[m])}${change ? ` (${change})` : ''}`;
    })].filter(Boolean).join(' · ') : '');
  const d = panel.detail;
  if (!d || d.iteration !== n) { // loading: nothing of the previous selection stays
    show('panel-summary', false);
    show('panel-reason', false);
    $('sections').replaceChildren();
    return;
  }
  setText('panel-summary', d.summary || '');
  show('panel-summary', !!d.summary);
  // Why it was rejected, in full, when no gate says it (agent error, integrity, ...).
  const gates = d.gates || [];
  const byGate = gates.some((g) => g.passed === false);
  const full = r?.accepted === false && !byGate ? r.reason || r.summary : null;
  // Not an agent error's: the title says it in the console's words (its output is under Activity).
  const reason = full && !why?.error && !(why && full.includes(why.text)) ? full : null;
  show('panel-reason', !!(reason || (!byGate && d.failure_output)));
  $('panel-reason').replaceChildren(...[reason && el('p', { textContent: reason }),
    !byGate && d.failure_output && el('pre', { textContent: d.failure_output })].filter(Boolean));

  const list = [];
  if (gates.length) list.push(gatesSection(d, gates, live, byGate));
  const changes = changesSection(d, live, outcome === 'accepted' || live);
  if (changes) list.push(changes);
  for (const o of d.observe || []) list.push(observeSection(d, o));
  if (d.stop?.length) list.push(stopSection(d));
  if (d.notes) {
    const s = section('note', 'Agent note', false);
    s.body.className = 'sec-body prose';
    setText(s.count, `· notes/iter-${n}.md`);
    setHTML(s.body, d.notes);
    list.push(s);
  }
  const activity = activitySection(d);
  if (activity) list.push(activity);
  const nodes = list.map((s) => s.node);
  for (const [key, s] of panel.sections) if (!list.includes(s)) panel.sections.delete(key);
  const box = $('sections');
  if (nodes.length !== box.children.length || nodes.some((node, k) => box.children[k] !== node)) box.replaceChildren(...nodes);
}

// Gates in list order: ✓ passed, ✗ failed with its output, ○ not run (after a failure,
// or not reached yet while the iteration runs); each with its stored output.
function gatesSection(d, gates, live, byGate) {
  const s = section('gates', 'Gates', byGate);
  const count = (v) => gates.filter((g) => g.passed === v).length;
  const words = [count(true) && `${count(true)} passed`, count(false) && `${count(false)} failed`,
    count(null) && `${count(null)} ${live ? 'to run' : 'not run'}`].filter(Boolean).join(' · ');
  fill(s, words, JSON.stringify([gates, d.failure_output, live]), () => gates.map((g, index) => {
    const outcome = g.passed === true ? 'passed' : g.passed === false ? 'failed' : 'not-run';
    const facts = [
      g.kind === 'metric' && isNumber(g.value) && `${fmtNumber(g.value)}${isNumber(g.reference) ? ` vs ${fmtNumber(g.reference)}` : ''}`,
      isNumber(g.duration_s) && fmtDuration(g.duration_s),
      g.passed === null && !live && 'not run',
    ].filter(Boolean).join(' · ');
    // A failed command's output: its stored files, opened; the record's excerpt without them (report.html).
    const failed = g.passed === false;
    const stored = g.stored?.files.some((f) => f.name !== 'command.txt' && f.size > 0);
    const output = failed && !stored ? g.output || (g.kind === 'command' ? d.failure_output : null) : null;
    return el('div', { className: `gate ${outcome}` },
      el('div', {}, el('span', { className: 'gate-mark', textContent: { passed: '✓', failed: '✗', 'not-run': '○' }[outcome] }),
        el('code', { textContent: g.text }), facts ? el('span', { className: 'muted', textContent: `  ${facts}` }) : null),
      g.passed === false && g.reason ? el('p', { className: 'gate-reason', textContent: g.reason }) : null,
      output ? el('pre', { textContent: output }) : null,
      storedEl(d.iteration, 'gates', index + 1, g.stored, failed));
  }));
  return s;
}

// This attempt's change: its own diff (start commit to frozen tree), or the edits so
// far while it runs. report.html keeps one diff, baseline to the final code.
function changesSection(d, live, defaultOpen) {
  const discarded = d.discarded || [];
  const own = panel.diff?.text;
  const text = own || d.diff;
  const sinceBaseline = !own && !!d.diff && !live; // report.html: baseline to the final code
  const error = text ? null : panel.diff?.error;
  const loading = !text && !!d.attempt && (!panel.diff || panel.diff.loading); // not asked for yet, or on its way
  if (!text && !discarded.length && !d.attempt) return null;
  const s = section('changes', 'Changes', defaultOpen);
  const files = text ? parseDiff(text) : [];
  const add = files.reduce((sum, f) => sum + f.add, 0), del = files.reduce((sum, f) => sum + f.del, 0);
  const count = [files.length === 1 ? files[0].path : files.length > 1 && plural(files.length, 'file'),
    files.length && `+${add} −${del}`, live && 'in progress', sinceBaseline && 'since the baseline',
    text?.includes('[… diff cut') && 'cut'].filter(Boolean).join(' · ');
  const frozen = new Set(d.discarded_frozen || []);
  fill(s, count, JSON.stringify([text, error, discarded, loading]), () => [
    discarded.length ? el('p', { className: 'muted' }, 'Outside the scope, discarded: ',
      ...discarded.flatMap((p, i) => [i ? ', ' : '', el('code', { textContent: p }), frozen.has(p) ? ' (frozen)' : ''])) : null,
    ...(files.length === 1 ? [diffEl(files[0])] : files.map(diffFileEl)),
    !text && el('p', { className: 'muted', textContent: error
      || (loading ? 'Loading…' : SNAPSHOT ? 'Not saved in this file; see autohelix view --web.' : 'No changes.') }),
  ].filter(Boolean));
  return s;
}

// One observe entry: its status and metrics, output (a review's as Markdown), files.
function observeSection(d, o) {
  const s = section(`observe:${o.name}`, o.name, false);
  const failed = o.ok === false;
  const metrics = Object.entries(o.metrics || {}).map(([k, v]) => `${k} ${fmtNumber(v)}`).join(' · ');
  fill(s, [o.review && 'review', failed && `failed${o.error ? `: ${o.error}` : ''}`, o.running && 'running', metrics,
    o.files.length && plural(o.files.length, 'file')].filter(Boolean).join(' · '), JSON.stringify(o), () => {
    const parts = [];
    if (failed) parts.push(el('p', { className: 'muted', textContent: `Failed${o.error ? `: ${o.error}` : ''}; its metrics are missing.` }));
    if (o.html) {
      const prose = el('div', { className: 'prose' });
      prose.innerHTML = o.html; // rendered by the server from Markdown with raw HTML disabled
      parts.push(prose);
    } else if (o.stdout) {
      parts.push(el('pre', { className: 'output', textContent: o.stdout }));
    } else if (o.stdout === null && SNAPSHOT) {
      parts.push(el('p', { className: 'muted', textContent: 'Output is not saved in this file; see autohelix view --web.' }));
    }
    if (o.stderr) parts.push(fileLike('stderr', el('pre', { textContent: o.stderr })));
    for (const a of o.files) parts.push(fileEl(artifactUrl(d.iteration, a.path), a.path.slice(o.name.length + 1), a.size));
    if (o.stdout_file && !SNAPSHOT) {
      parts.push(el('a', { className: 'raw', href: artifactUrl(d.iteration, o.stdout_file), target: '_blank', rel: 'noopener', textContent: 'stdout.txt' }));
    }
    return parts;
  });
  return s;
}

// The stop commands that ran after this iteration (accepted ones only): ■ the one that stopped the run.
function stopSection(d) {
  const s = section('stop', 'Stop', false);
  const stops = d.stop;
  fill(s, `${stops.length} ran${stops.some((x) => x.stopped) ? ' · stopped the run' : ''}`, JSON.stringify(stops),
    () => stops.map((x) => el('div', { className: `gate ${x.stopped ? 'stopped' : 'passed'}` },
      el('div', {}, el('span', { className: 'gate-mark', textContent: x.stopped ? '■' : '·' }),
        el('code', { textContent: x.command || `stop/${x.position}` }),
        x.stopped ? el('span', { className: 'muted', textContent: '  stopped the run' }) : null),
      storedEl(d.iteration, 'stop', x.position, x.stored))));
  return s;
}

// The agent's and judges' calls, live while they run; headed by role when there are several.
function activitySection(d) {
  const calls = (d.calls || []).filter((call) => call.events.length > 0);
  if (!calls.length) return null;
  const s = section('activity', 'Activity', false);
  const shown = steps(calls.flatMap((call) => call.events));
  const tools = shown.filter((x) => x.type === 'tool').length;
  const says = shown.filter((x) => x.type === 'say').length;
  setText(s.count, [tools && plural(tools, 'tool call'), says && plural(says, 'message'),
    d.agent_clipped && 'earliest activity not shown'].filter(Boolean).map((x) => `· ${x}`).join(' '));
  if (s.body.childElementCount !== calls.length) {
    s.body.replaceChildren(...calls.map(() => el('div', {}, el('h4'), el('div', { className: 'log' }))));
  }
  calls.forEach((call, index) => {
    const [heading, log] = s.body.children[index].children;
    setText(heading, calls.length > 1 ? `${index + 1} · ${call.usage?.source || call.role}` : '');
    const key = call.log || call.live_log || `${call.role}-${index}`;
    if (!panel.expanded.has(key)) panel.expanded.set(key, new Set());
    const eventsKey = JSON.stringify(call.events);
    if (log.dataset.key !== eventsKey) { log.dataset.key = eventsKey; renderEvents(log, call.events, panel.expanded.get(key)); }
  });
  return s;
}

// A gate's or stop command's stored files (loaded when opened) and its ask/review transcripts.
function storedEl(iteration, kind, k, stored, openOutput = false) {
  if (!stored) return null;
  // command.txt is the command the row names already.
  const items = stored.files.filter((f) => f.name !== 'command.txt').map((f) => fileEl(
    api(`/api/output/${iteration}/${kind}/${k}/${encodeURIComponent(f.name)}`), f.name, f.size,
    { id: `${kind}/${k}/${f.name}`, headTail: true, open: openOutput && f.size > 0 }));
  for (const t of stored.transcripts) {
    const id = `${kind}/${k}/${t.name}`;
    const log = el('div', { className: 'log' });
    if (!panel.expanded.has(id)) panel.expanded.set(id, new Set());
    renderEvents(log, t.events, panel.expanded.get(id));
    items.push(fileLike(t.name + (t.events.length ? ` · ${plural(t.events.length, 'event')}` : ''), log, id));
  }
  return items.length ? el('div', { className: 'stored' }, ...items) : null;
}

// ---------- files and diffs ----------

function fmtSize(bytes) {
  return bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
}

// A file in iterations/NNN/observe/, by its path there; each component encoded on its own.
function artifactUrl(iteration, path) {
  return api(`/api/artifact/${iteration}/${path.split('/').map(encodeURIComponent).join('/')}`);
}

// A folding row with content already at hand; with `id`, as the user left it across redraws.
function fileLike(name, content, id = null) {
  const item = el('details', { className: 'file', open: !!id && !!panel.files.get(id) },
    el('summary', { textContent: name }), el('div', { className: 'file-body' }, content));
  if (id) item.addEventListener('toggle', () => panel.files.set(id, item.open));
  return item;
}

// A file loaded when its row is opened. `headTail` keeps both ends of a long text
// (gate output: errors at the top, summaries at the bottom), else its start.
function fileEl(url, name, size, { id = null, headTail = false, open = false } = {}) {
  const body = el('div', { className: 'file-body' });
  const item = el('details', { className: 'file', open: (id && panel.files.get(id)) ?? open },
    el('summary', {}, name, el('span', { className: 'muted', textContent: ` · ${fmtSize(size)}` })), body);
  let loading = false;
  const load = async () => {
    if (!item.open || loading || body.childElementCount) return;
    loading = true;
    try {
      if (/\.(png|jpe?g|gif|webp)$/i.test(name)) {
        body.append(el('img', { src: url, alt: name }));
      } else {
        const text = await (await fetch(url, { cache: 'no-store' })).text();
        const cut = text.length <= 262144 ? text : headTail
          ? `${text.slice(0, 65536)}\n[… ${fmtSize(text.length - 262144)} not shown …]\n${text.slice(-196608)}`
          : text.slice(0, 262144) + '\n…';
        body.append(el('pre', { textContent: cut }));
      }
      body.append(el('a', { href: url, target: '_blank', rel: 'noopener', textContent: 'Open in new tab' }));
    } catch { /* opened again, it retries */ } finally { loading = false; }
  };
  item.addEventListener('toggle', () => {
    if (id) panel.files.set(id, item.open);
    load();
  });
  if (item.open) load();
  return item;
}

// git diff text -> files with their rows (old/new line numbers); unchanged stretches
// between changed regions as "⋯ N unchanged lines" instead of git's @@ headers.
function parseDiff(text) {
  const files = [];
  let file = null, oldNo = 0, newNo = 0, lastNew = 0;
  for (const line of text.split('\n')) {
    if (line.startsWith('[… diff cut')) { // the server stopped reading there
      file?.rows.push(['gap', line.replace(/^\[|\]$/g, '')]);
    } else if (line.startsWith('diff --git ')) {
      file = { path: line.replace(/^diff --git .* b\/(.*)$/, '$1'), rows: [], add: 0, del: 0 };
      files.push(file);
      lastNew = 0;
    } else if (!file || line === '' || /^(index |--- |\+\+\+ |new file mode|deleted file mode|similarity |rename |\\ No newline)/.test(line)) {
      continue; // the end (empty context lines are " "), or headers
    } else if (line.startsWith('@@')) {
      const m = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (!m) continue;
      [oldNo, newNo] = [Number(m[1]), Number(m[2])];
      const skipped = newNo - lastNew - 1;
      if (skipped > 0) file.rows.push(['gap', `⋯ ${skipped} unchanged line${skipped === 1 ? '' : 's'}`]);
    } else if (line[0] === '+') {
      file.add++; lastNew = newNo;
      file.rows.push(['add', '', newNo++, '+', line.slice(1)]);
    } else if (line[0] === '-') {
      file.del++;
      file.rows.push(['del', oldNo++, '', '−', line.slice(1)]);
    } else {
      lastNew = newNo;
      file.rows.push(['ctx', oldNo++, newNo++, '', line.slice(1)]);
    }
  }
  return files;
}

// One file's rows: old and new line numbers, sign, code.
function diffEl(f) {
  return el('pre', { className: 'diff' }, ...f.rows.map(([kind, a, b, sign, code]) => (kind === 'gap'
    ? el('div', { className: 'gap', textContent: a })
    : el('div', { className: `line ${kind}` }, el('span', { className: 'ln', textContent: a }),
      el('span', { className: 'ln', textContent: b }), el('span', { className: 'sign', textContent: sign }),
      el('span', { className: 'code', textContent: code })))));
}

// Of several files, each folds on its own (`path +a −d`, folded first); kept by path.
function diffFileEl(f) {
  const item = el('details', { className: 'diff-file', open: panel.diffOpen.get(f.path) ?? false },
    el('summary', {}, el('code', { textContent: f.path }), el('span', { className: 'muted', textContent: ` +${f.add} −${f.del}` })),
    diffEl(f));
  item.addEventListener('toggle', () => panel.diffOpen.set(f.path, item.open));
  return item;
}

// ---------- activity ----------

function shortCommand(text) {
  // Codex wraps commands as: /bin/zsh -lc '...'
  const m = text.match(/^\/bin\/(?:ba|z)?sh -lc (['"])([\s\S]*)\1$/);
  return m ? m[2] : text;
}

function summarizeInput(text) {
  try {
    const v = JSON.parse(text);
    return v.command || v.file_path || v.pattern || v.path || v.url || v.query || v.description || text;
  } catch { return text; }
}

// Group the event stream into readable steps: prose, and collapsible tool calls.
function steps(events) {
  const out = [];
  let tool = null;     // newest call: receives its streamed input
  const waiting = [];  // calls without a result yet; results arrive in call order
  for (const e of events) {
    if (e.kind === 'tool_use') {
      tool = { type: 'tool', name: e.text, input: '', result: '', error: false };
      out.push(tool);
      waiting.push(tool);
    } else if (e.kind === 'tool_input' && tool) {
      tool.input = summarizeInput(e.text);
    } else if (e.kind === 'tool_result' && (waiting.length || tool)) {
      const target = waiting.shift() || tool;
      target.result += (target.result ? '\n' : '') + e.text;
      target.error ||= e.error;
    } else if (e.kind === 'thinking' && !e.text) {
      tool = null;
      if (out.at(-1)?.type !== 'hidden-think') out.push({ type: 'hidden-think' }); // text not logged
    } else if (e.kind === 'assistant_text' || e.kind === 'thinking') {
      tool = null;
      if (e.kind === 'thinking' && out.at(-1)?.type === 'hidden-think') out.pop(); // its text did arrive
      out.push({ type: e.kind === 'thinking' ? 'think' : 'say', text: e.text, html: e.html });
    } else if (e.kind === 'warning' || e.kind === 'error') {
      out.push({ type: 'error', text: e.text });
    }
    // status/result events are lifecycle and usage bookkeeping; skip them.
  }
  return out;
}

function renderEvents(box, events, expanded) {
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.replaceChildren(...steps(events).map((s, i) => {
    if (s.html) {
      const node = el('div', { className: 'ev say prose' });
      node.innerHTML = s.html; // Server-rendered Markdown, raw HTML disabled.
      return node;
    }
    if (s.type === 'hidden-think') return el('div', { className: 'ev think', textContent: 'Thinking (not logged)' });
    if (s.type !== 'tool') return el('div', { className: `ev ${s.type}`, textContent: s.text });
    // Claude: name "Bash" + input; Codex: the name is the command itself.
    const command = shortCommand(s.input || s.name);
    const first = command.split('\n')[0];
    const text = s.input ? `${s.name}  ${first}` : first;
    const body = [command.includes('\n') ? command : null, s.result || null].filter(Boolean).join('\n\n');
    const node = el('details', { className: `ev tool${s.error ? ' error' : ''}`, open: expanded.has(i) },
      el('summary', { textContent: text.length > 160 ? text.slice(0, 160) + '…' : text }),
      body ? el('pre', { textContent: body.length > 4000 ? body.slice(0, 4000) + '\n…' : body }) : null);
    node.addEventListener('toggle', () => (node.open ? expanded.add(i) : expanded.delete(i)));
    return node;
  }));
  if (atBottom) box.scrollTop = box.scrollHeight;
}

// ---------- config strip ----------

// Light YAML highlighting: keys tinted, comments dimmed. Text inside block scalars
// (`goal: |`) is literal, so a "#" there is not a comment.
function highlightYaml(text) {
  const nodes = [];
  let blockIndent = null; // indentation of the key that opened a block scalar
  for (const line of text.split('\n')) {
    const indent = line.match(/^ */)[0].length;
    if (blockIndent !== null && (line.trim() === '' || indent > blockIndent)) {
      nodes.push(line + '\n');
      continue;
    }
    blockIndent = null;
    let comment = -1, quote = null;
    for (let k = 0; k < line.length; k++) {
      const ch = line[k];
      if (quote) { if (ch === quote) quote = null; }
      else if (ch === '"' || ch === "'") quote = ch;
      else if (ch === '#' && (k === 0 || /\s/.test(line[k - 1]))) { comment = k; break; }
    }
    const code = comment < 0 ? line : line.slice(0, comment);
    const key = code.match(/^(\s*(?:-\s+)?)([\w.-]+)(:)(.*)$/);
    if (key) {
      nodes.push(key[1], el('span', { className: 'yaml-key', textContent: key[2] }), key[3] + key[4]);
      if (/^\s*[|>][+-]?\s*$/.test(key[4])) blockIndent = indent;
    } else {
      nodes.push(code);
    }
    if (comment >= 0) nodes.push(el('span', { className: 'yaml-comment', textContent: line.slice(comment) }));
    nodes.push('\n');
  }
  return nodes;
}

// "bench (speed ↑)", as the console header lists observe entries.
function observeLabel(o) {
  const metrics = Object.entries(o.metrics || {}).map(([k, dir]) => `${k} ${dir === 'lower' ? '↓' : '↑'}`);
  return metrics.length ? `${o.name} (${metrics.join(', ')})` : o.name;
}

function renderConfig() {
  const c = state.config;
  show('config', !!(c?.text || c?.missing));
  if (!c?.text && !c?.missing) return;
  const summary = $('config-summary');
  const pre = $('config-text');
  if (c.missing) { // a run started by an older AutoHelix: never its source file instead
    summary.title = '';
    rebuild(summary, c.file, () => [el('b', { textContent: 'Config' }),
      el('span', { className: 'config-file', textContent: `${c.file}: not stored with this run` })]);
    pre.replaceChildren();
    return;
  }
  // Budgets are not config: the latest segment's flags, as its status records them.
  // Iterations and cost as run totals (spent before it plus its own cap); --time only
  // while the segment runs (what is left of it is under the Time stat).
  const s = state.status || {};
  const budget = [
    s.max_iterations && `to iteration ${s.max_iterations}`,
    s.max_cost_usd && `${fmtCap(s.max_cost_usd)} total`,
    s.state === 'running' && isNumber(s.max_time_seconds) && s.max_time_seconds > 0 && `--time ${fmtLimit(s.max_time_seconds)}`,
  ].filter(Boolean).join(' · ');
  const parts = [
    c.agent && ['Agent', c.agent],
    c.gates?.length && ['Gates', c.gates.join(' · '), c.gates.join('\n')],
    c.observe?.length && ['Observe', c.observe.map(observeLabel).join(' · ')],
    c.scope?.length && ['Editable', c.scope.join(', ')],
    budget && ['Budget', budget, `The latest run/resume's flags: ${budget} (iterations and cost as run totals, --time its own)`],
  ].filter(Boolean);
  summary.title = `Show the full ${c.file}`;
  rebuild(summary, JSON.stringify([c.file, parts]), () => [el('b', { textContent: 'Config' }),
    el('span', { className: 'config-file', textContent: c.file }),
    // One line; long values are cut with "…" (hover, or open the config, for the full text).
    ...parts.map(([k, v, full]) => el('span', { className: 'chip', title: full || v }, el('b', { textContent: k + ' ' }), v))]);
  if (pre.dataset.text !== c.text) { pre.dataset.text = c.text; pre.replaceChildren(...highlightYaml(c.text)); }
}

// ---------- page ----------

async function getJSON(url) {
  if (SNAPSHOT) {
    if (url in SNAPSHOT.routes) return SNAPSHOT.routes[url];
    throw new Error(`${url}: not in this file`);
  }
  const res = await fetch(api(url), { cache: 'no-store' });
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  return res.json();
}

function renderHeading() {
  // The page is about one run: its id heads it (name and suffix, as typed in `resume 1q3w`;
  // the start time muted beside them), the goal below; the project above is the way back.
  const project = state.project;
  const { name, started } = splitRunId(state.run_id);
  setText('title', name || project);
  setText('title-date', started);
  if (state.run_id) $('title').title = state.run_id;
  document.title = `${name || project} · AutoHelix${SNAPSHOT ? ' report' : ''}`;
  if (SNAPSHOT) {
    show('kind', true);
    setText('kind', [project, `run report · saved ${fmtClock(SNAPSHOT.saved_at)}`].filter(Boolean).join(' · '));
  } else if (state.run_id) { show('crumbs', true); setText('crumb-project', project); }
  const goal = (state.goal || '').trim();
  setText('goal', goal);
  show('goal-more', goal.includes('\n') || $('goal').scrollWidth > $('goal').clientWidth);
  // Why the run exists (run --why), what it continues from (run --import), why it was archived.
  const why = runNotes(state);
  setText('why', why);
  show('why', !!why);
}

function render() {
  const names = metricNames();
  if (!names.includes(metric)) metric = names[0] || null;
  renderHeading();
  renderStatus();
  renderConfig();
  renderReport();
  renderStats();
  $('metric').hidden = names.length < 2; // the chart's menu, for more than one metric
  fillMenu($('metric'), names, metric);
  renderChart();
  renderRows();
  renderPanel();
  renderSharedNotes();
}

async function loop() {
  try {
    state = await getJSON('/api/state');
  } catch {
    $('status').textContent = 'Viewer disconnected — retrying';
    $('status').className = 'status error';
    if (!SNAPSHOT) setTimeout(loop, POLL_MS);
    return;
  }
  try {
    // Follow the run: the running (or latest) iteration until the user picks another.
    const target = liveIteration() ?? state.iterations.at(-1)?.iteration ?? null;
    if (selected !== null && !iterationOrder().includes(selected)) { // a stale or mistyped #iter-N
      followLive = true;
      history.replaceState(null, '', location.pathname);
    }
    if (target !== null && (selected === null || (followLive && target !== selected))) select(target, false);
    else { render(); await loadDetail(); }
  } catch (err) {
    console.error(err); // a rendering bug; keep polling
  } finally {
    if (!SNAPSHOT) setTimeout(loop, document.hidden ? POLL_MS * 5 : POLL_MS);
  }
}

themeButton(SNAPSHOT?.logos);
$('metric').addEventListener('change', (e) => { metric = e.target.value; render(); });
$('goal-more').addEventListener('click', () => {
  const open = $('goal').classList.toggle('expanded');
  $('goal-more').textContent = open ? 'less' : 'more';
});
$('prev').addEventListener('click', () => step(-1));
$('next').addEventListener('click', () => step(1));
document.addEventListener('keydown', (e) => {
  if (!state || ['SELECT', 'INPUT', 'TEXTAREA'].includes(e.target.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === 'j') step(1); // down (newer)
  if (e.key === 'k') step(-1); // up (older)
});
const linked = location.hash.match(/^#iter-(\d+)$/);
if (linked) { selected = Number(linked[1]); followLive = false; panel = newPanel(selected); }
loop();
