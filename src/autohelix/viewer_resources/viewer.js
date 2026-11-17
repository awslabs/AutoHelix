// AutoHelix run page: one run's .autohelix/ state. Polls /api/state, and the selected
// iteration's details for the panel beside the table; after the last iteration the panel
// can show the run's summary (report.md). In report.html the same routes come from data
// built into the file, and nothing is polled. Shared helpers: common.js.
'use strict';

const POLL_MS = 2000;
const SNAPSHOT = window.AUTOHELIX_SNAPSHOT || null;
// On /run/<id>/ the routes are /api/run/<id>/...; the snapshot keys them as /api/....
const RUN_PAGE = SNAPSHOT ? null : location.pathname.match(/^\/run\/([a-z0-9][a-z0-9-]*)\/$/);
const api = (route) => (RUN_PAGE ? route.replace(/^\/api\//, `/api/run/${RUN_PAGE[1]}/`) : route);
const NARROW = matchMedia('(max-width: 1100px)'); // one column: the panel follows the table
const REPORT = 'report'; // the panel's last page, after the iterations: the run's summary

let state = null;
let selected = null;   // what the panel shows: an iteration, or REPORT
let followLive = true; // keep selecting the running iteration (or the summary) until the user picks another
let metric = null;
const opened = new Map(); // section -> open, as the user left it (kept across selections)

const label = (n) => (n === REPORT ? 'Summary' : n === 0 ? 'Baseline' : `Iteration ${n}`);
const row = (n) => state.iterations.find((r) => r.iteration === n);
const baselineValue = () => row(0)?.metrics?.[metric];

function show(id, visible) { (typeof id === 'string' ? $(id) : id).hidden = !visible; }

// Only for HTML the server rendered from Markdown with raw HTML disabled.
function setHTML(node, html) {
  if (node.dataset.html !== html) { node.dataset.html = html; node.innerHTML = html; }
}

// Replace a node's children only when `key` (what they are made from) changed: hover
// titles, focus and open folds survive the polls that change nothing.
function rebuild(node, key, build) {
  if (node.dataset.key === key) return false;
  node.dataset.key = key;
  node.replaceChildren(...build());
  return true;
}

function deltaEl(value, baseline, wrap = true) {
  const text = formatDelta(value, baseline);
  if (!text) return null;
  const dir = state.directions[metric];
  const cls = !dir ? '' : better(value, baseline, dir) ? 'better' : 'worse';
  return el('span', { className: `delta ${cls}`, textContent: wrap ? ` (${text})` : text });
}

// Where an iteration's parts are stored, below .autohelix/ (section tooltips).
function iterDir(i) {
  return `runs/${state?.run_id || '<id>'}/iterations/${String(i).padStart(3, '0')}`;
}

// ---------- status ----------

function liveIteration() {
  const s = state?.status;
  return s?.state === 'running' && s.phase !== 'report' && s.iteration !== null ? s.iteration : null;
}

// Running: `view`'s status line (the server's: `Running · iteration 3 · gates · 12s`).
function renderStatus() {
  const s = state.status;
  const node = $('status');
  if (!s) {
    node.textContent = 'No run recorded';
    node.className = 'status idle';
    return;
  }
  if (s.state === 'running') {
    node.textContent = s.line + (state.stop_requested ? ' · stopping (autohelix stop)' : '');
    node.title = '';
  } else {
    const when = s.stopped_at ? ` at ${fmtClock(s.stopped_at)}` : '';
    const words = { finished: 'Finished', interrupted: 'Interrupted', failed: 'Failed', crashed: 'Stopped unexpectedly' };
    node.textContent = (words[s.state] || s.state) + when;
    // Why it ended (a budget, a stop command, `autohelix stop`) on hover: a stop command can be long.
    node.title = s.stop_reason?.detail || '';
  }
  node.className = `status ${s.state}`;
}

// ---------- summary (report.md), shared notes ----------

// The summary, in the panel when it is selected.
function renderReport() {
  const r = state.report;
  const body = $('report-body');
  const on = selected === REPORT && !!r;
  show(body, on);
  if (!on || body.dataset.html === r.html) return;
  setHTML(body, r.html);
  linkIterations(body);
}

// "iter N" in the summary selects that iteration.
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
  setText('shared-count', files.length ? `· ${plural(files.length, 'file')}` : '');
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

// ---------- overview ----------

function metricNames() {
  const names = new Set(Object.keys(state.directions || {}));
  for (const r of state.iterations) Object.keys(r.metrics || {}).forEach((k) => names.add(k));
  return [...names];
}

// Running best over accepted iterations, used for both the chart and the headline.
function bestSoFar() {
  const dir = state.directions[metric] || 'higher';
  let best = null;
  const out = new Map();
  for (const r of state.iterations) {
    const v = r.metrics?.[metric];
    if (r.accepted && isNumber(v) && (best === null || better(v, best, dir))) best = v;
    out.set(r.iteration, best);
  }
  return out;
}

// Best metric, then run totals (as in the terminal's run summary): time over the
// iterations (finished, and the running one so far); cost also includes the baseline's calls.
function renderStats() {
  const its = state.iterations.filter((r) => r.iteration > 0);
  const cost = state.cost;
  const stats = [];
  if (metric) {
    const best = [...bestSoFar().values()].at(-1);
    stats.push([`Best ${metric}`, fmtNumber(best), deltaEl(best, baselineValue())]);
  }
  // The running iteration counts from its start, so the total moves while it runs.
  const live = liveIteration();
  const started = state.status?.iteration_started_at;
  const running = live > 0 && !row(live) && isNumber(started) ? Math.max(0, Date.now() / 1000 - started) : null;
  if (its.length || running !== null) {
    const timed = its.filter((r) => isNumber(r.seconds));
    stats.push(['Time', timed.length || running !== null
      ? fmtDuration(timed.reduce((sum, r) => sum + r.seconds, running ?? 0)) + (timed.length < its.length ? ' (incomplete)' : '')
      : '—']);
  }
  if (its.length || isNumber(cost?.cost_usd) || cost?.incomplete) {
    stats.push(['Cost', isNumber(cost?.cost_usd)
      ? fmtMoney(cost.cost_usd) + (cost.incomplete ? ' (incomplete)' : '')
      : 'unavailable']);
  }
  $('stats').replaceChildren(...stats.map(([k, v, extra]) => el('div', { className: 'stat' },
    el('span', { className: 'stat-label', textContent: k }),
    el('span', { className: 'stat-value', textContent: v }, extra))));
}

function renderMetricSelect(names) {
  const select = $('metric'); // only shown when there is more than one metric
  select.hidden = names.length < 2;
  fillMenu(select, names, metric);
  setText('metric-col', metric || '');
}

// ---------- chart ----------

function svgEl(tag, attrs, text) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

// Every measured value as a dot (hollow: rejected), the best accepted value so far as a
// step line carried on to the running iteration (a ring at its end). Click a dot to select it.
function renderChart() {
  const svg = $('chart');
  const points = metric ? state.iterations.filter((r) => isNumber(r.metrics?.[metric])) : [];
  show('chart-box', points.length > 0); // `hidden` does not apply to <svg> itself
  if (!points.length) return;
  const W = 640, H = 200, pad = { l: 52, r: 12, t: 10, b: 22 };
  const values = points.map((r) => r.metrics[metric]);
  const best = bestSoFar();
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
  const step = Math.ceil(n / 10); // whole iterations, evenly spaced
  for (let i = 0; i <= n; i += step) {
    children.push(svgEl('text', { x: x(i), y: H - 4, 'text-anchor': 'middle', class: 'axis' }, String(i)));
  }
  let d = '';
  for (const r of state.iterations) {
    const b = best.get(r.iteration);
    if (b === null) continue;
    d += d ? ` H${x(r.iteration)} V${y(b)}` : `M${x(r.iteration)} ${y(b)}`;
  }
  if (d && live !== null) d += ` H${x(n)}`;
  children.push(svgEl('path', { d, class: 'best-line' }));
  const dot = (i, v, cls, title) => {
    const node = svgEl('circle', { cx: x(i), cy: y(v), r: i === selected ? 6 : 4, class: `dot ${cls}${i === selected ? ' selected' : ''}` });
    node.append(svgEl('title', {}, title));
    node.addEventListener('click', () => select(i));
    children.push(node);
  };
  const bestNow = best.get(last);
  if (live !== null && live > last && bestNow !== null) dot(live, bestNow, 'live', `${label(live)}: running`);
  for (const r of points) {
    dot(r.iteration, r.metrics[metric], r.accepted ? 'accepted' : 'rejected', `${label(r.iteration)}: ${fmtNumber(r.metrics[metric])}`);
  }
  rebuild(svg, JSON.stringify([metric, points.map((r) => [r.iteration, r.metrics[metric], r.accepted]), live, last, selected]),
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

// One row per iteration (the selected metric with its change against the baseline), the
// running one, and the summary last when the run has one. Rebuilt only when the rows
// change (a focused row keeps its focus across polls).
function renderRows() {
  const base = baselineValue();
  const live = liveIteration();
  const running = live !== null && !row(live) ? live : null; // the row under way, not recorded yet
  const report = state.report;
  const box = $('rows');
  rebuild(box, JSON.stringify([metric, state.iterations, running, running !== null && state.status.phase,
    report && [report.written_at, report.last_iteration, report.seconds, report.cost_usd]]), () => {
    const rows = state.iterations.map((r) => {
      const v = r.metrics?.[metric];
      const outcome = r.iteration === 0 ? 'baseline' : r.accepted ? 'accepted' : 'rejected';
      return rowEl(r.iteration, [
        el('td', { textContent: String(r.iteration) }),
        el('td', {}, el('span', { className: `pill ${outcome}`, textContent: outcome })),
        el('td', { className: 'value' }, metric ? fmtNumber(v) : '', metric && r.iteration > 0 ? deltaEl(v, base) : null),
        summaryCell(r),
        el('td', { className: 'num', textContent: fmtDuration(r.seconds) }),
        el('td', { className: 'num', textContent: (fmtMoney(r.cost) || '—') + (isNumber(r.cost) && r.cost_incomplete ? ' (incomplete)' : ''),
          title: r.cost_incomplete ? 'Some call costs are unavailable' : '' }),
      ]);
    });
    if (running !== null) {
      rows.push(rowEl(running, [
        el('td', { textContent: String(running) }),
        el('td', {}, el('span', { className: 'pill running', textContent: 'running' })),
        el('td'), el('td', { className: 'summary muted', textContent: phaseText(state.status.phase) }), el('td'), el('td'),
      ]));
    }
    if (report) {
      rows.push(rowEl(REPORT, [
        el('td'),
        el('td', {}, el('span', { className: 'pill summary', textContent: 'summary' })),
        el('td'),
        el('td', { className: 'summary', textContent: [report.last_iteration && `Iterations 1–${report.last_iteration}`,
          report.written_at && `written ${fmtClock(report.written_at)}`].filter(Boolean).join(', ') }),
        el('td', { className: 'num', textContent: fmtDuration(report.seconds) }),
        el('td', { className: 'num', textContent: fmtMoney(report.cost_usd) }),
      ]));
    }
    return rows;
  });
  for (const tr of box.children) tr.classList.toggle('selected', tr.dataset.iteration === String(selected));
}

// Rejected rows: why (the failing gate), then the agent's one-line summary under it.
// Observe failures and discarded changes are flagged; the details are in the panel.
function summaryCell(r) {
  const why = r.accepted === false ? rejection(r) : '';
  const summary = r.summary && r.summary !== r.reason ? r.summary.split('\n')[0] : '';
  const failed = (r.observe || []).filter((o) => o.ok === false).map((o) => o.name);
  const baselineGates = r.iteration === 0
    ? (r.gates || []).filter((g) => g.kind === 'command' && g.passed === false).map((g) => g.text) : [];
  const flags = [
    baselineGates.length && `gate failed at the baseline: ${baselineGates.join(', ')}`,
    failed.length && `${failed.join(', ')} failed`,
    r.discarded?.length && `${plural(r.discarded.length, 'out-of-scope change')} discarded`,
  ].filter(Boolean).join(' · ');
  return el('td', { className: 'summary' },
    why ? el('div', { className: 'why', textContent: why }) : null,
    summary ? el('div', { textContent: summary }) : null,
    flags ? el('div', { className: 'flag', textContent: flags }) : null);
}

// A rejection in a few words: the failing gate's text, else the harness's reason.
function rejection(r) {
  const gate = (r.gates || []).find((g) => g.passed === false);
  if (gate) return gate.text.length > 60 ? `✗ ${gate.text.slice(0, 60)}…` : `✗ ${gate.text}`;
  return shortReason(r.reason || r.summary);
}

// Harness rejection reasons, shortened for the panel's title (the full text is in "Why rejected").
function shortReason(reason) {
  if (!reason) return '';
  const check = reason.match(/^(?:constraint|gate) failed: (.*)$/s);
  if (check) {
    // Name the check by its tool: "python -m pytest …" -> "pytest failed".
    const cmd = check[1].trim();
    if (cmd.startsWith('ask: ')) return 'ask: no';
    const name = cmd.match(/^python3? -m (\S+)/)?.[1] || (/^python3? -c /.test(cmd) ? 'inline check' : cmd.split(/\s/)[0].split('/').pop());
    return `${name} failed`;
  }
  const gate = reason.match(/^metric gate: (\S+) (.*)$/s);
  if (gate) {
    const worse = gate[2].match(/is ([\d.]+%) worse/);
    if (worse) return `${gate[1]} ${worse[1]} worse`;
    return gate[2].startsWith('was not measured') ? `${gate[1]} not measured` : `${gate[1]} not improved enough`;
  }
  return reason.length > 40 ? reason.slice(0, 40) + '…' : reason;
}

const PHASES = {
  starting: 'Starting…', baseline: 'Measuring the baseline…', agent: 'Agent is working…',
  checks: 'Running gates…', observe: 'Running observe entries…',
  merge: 'Merging the accepted change…', 'between iterations': 'Wrapping up…',
};
const phaseText = (phase) => PHASES[phase] || `${phase}…`;

function rowEl(iteration, cells) {
  const tr = el('tr', { tabIndex: 0 }, ...cells);
  tr.dataset.iteration = String(iteration);
  tr.addEventListener('click', () => select(iteration));
  tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') select(iteration); });
  return tr;
}

// ---------- selection ----------

// What ← and → step through: the iterations, the running one, then the summary.
function iterationOrder() {
  const order = state.iterations.map((r) => r.iteration);
  const live = liveIteration();
  if (live !== null && !order.includes(live)) order.push(live);
  if (state.report) order.push(REPORT);
  return order;
}

// What the panel follows until the user picks something else: the running iteration;
// once the run stopped, its summary when it covers the last iteration, else that iteration.
function defaultTarget() {
  const live = liveIteration();
  if (live !== null) return live;
  const last = state.iterations.at(-1)?.iteration ?? null;
  const r = state.report;
  if (r && (last === null || !r.last_iteration || r.last_iteration >= last)) return REPORT;
  return last;
}

const hashOf = (n) => (n === REPORT ? '#summary' : `#iter-${n}`);

// `byUser` (a row, a dot, ← →, j/k): on the one-column layout, bring the panel into view.
function select(iteration, byUser = true) {
  if (iteration === undefined || iteration === null || !state) return;
  followLive = iteration === defaultTarget();
  if (iteration !== selected) {
    selected = iteration;
    panel = newPanel(iteration);
  }
  if (byUser) history.replaceState(null, '', followLive ? location.pathname : hashOf(iteration));
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
// One iteration: its title (outcome, why in a few words), the agent's summary, then
// folding sections: Why rejected, Discarded changes, Gates, Agent note, Changes in this
// iteration, Changes vs baseline, one per observe entry, Stop checks, Call activity.
// A section the user opened or closed stays so for the next iteration.

let panel = newPanel(null);

function newPanel(iteration) {
  return { iteration, key: undefined, detail: null, sections: new Map(), files: new Map(), diffOpen: new Map(),
           expanded: new Map(), diff: null };
}

function section(key, title, defaultOpen = false, source = null) {
  let s = panel.sections.get(key);
  if (!s) {
    const count = el('span', { className: 'muted' });
    const summary = el('summary', { title: source ? `.autohelix/${source}` : '' }, el('b', { textContent: title }), ' ', count);
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
  s.body.replaceChildren(...build().filter(Boolean));
}

async function loadDetail() {
  const n = selected;
  if (n === null || n === REPORT || !state) return;
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
    : { key: SNAPSHOT ? key : null, text: null, error: SNAPSHOT ? 'Not saved in this file; see autohelix view.' : 'Could not load the diff.' };
  renderPanel();
}

function renderPanel() {
  const n = selected;
  show('panel', n !== null);
  if (n === null) return;
  const order = iterationOrder();
  $('prev').disabled = order.indexOf(n) <= 0;
  $('next').disabled = order.indexOf(n) >= order.length - 1;
  renderReport();
  if (n === REPORT) {
    const r = state.report;
    // As an iteration's title: its name and one qualifier (when it was written, time and cost are in its row).
    $('panel-title').replaceChildren(el('span', { textContent: 'Summary' }),
      r?.last_iteration ? el('span', { className: 'muted', textContent: `iterations 1–${r.last_iteration}` }) : null);
    $('panel-title').title = `.autohelix/runs/${state.run_id}/report.md`;
    show('panel-summary', false);
    $('sections').replaceChildren();
    return;
  }
  $('panel-title').title = '';
  const r = row(n);
  const live = !r && n === liveIteration();
  const outcome = live ? 'running' : !r ? '' : n === 0 ? 'baseline' : r.accepted ? 'accepted' : 'rejected';
  const why = outcome === 'rejected' ? rejection(r) : '';
  $('panel-title').replaceChildren(el('span', { textContent: label(n) }), ...[
    n > 0 && outcome && el('span', { className: `pill ${outcome}`, textContent: outcome }),
    live && el('span', { className: 'muted', textContent: phaseText(state.status.phase) }),
    why && el('span', { className: 'muted', textContent: why }),
  ].filter(Boolean));
  const d = panel.detail;
  if (!d || d.iteration !== n) { // loading: nothing of the previous selection stays
    show('panel-summary', false);
    $('sections').replaceChildren();
    return;
  }
  setText('panel-summary', d.summary || '');
  show('panel-summary', !!d.summary);
  const where = iterDir(n);
  const list = [
    failureSection(d, r, where),
    discardedSection(d, where),
    d.gates?.length && gatesSection(d, where),
    d.notes && noteSection(d, live, where),
    d.attempt && attemptSection(d),
    d.diff && baselineDiffSection(d, live),
    ...(d.observe || []).map((o) => observeSection(d, o, where)),
    d.stop?.length && stopSection(d, where),
    activitySection(d, where),
  ].filter(Boolean);
  for (const [key, s] of panel.sections) if (!list.includes(s)) panel.sections.delete(key);
  const nodes = list.map((s) => s.node);
  const box = $('sections');
  if (nodes.length !== box.children.length || nodes.some((node, k) => box.children[k] !== node)) box.replaceChildren(...nodes);
}

// The harness's reason in full, and the failing command's output.
function failureSection(d, r, where) {
  const reason = r?.accepted === false ? r.reason || r.summary : null;
  if (!reason && !d.failure_output) return null;
  const s = section('failure', 'Why rejected', false, `${where}/record.json`);
  fill(s, '', JSON.stringify([reason, d.failure_output]), () => [
    reason && el('p', { className: 'reason', textContent: reason }),
    d.failure_output && el('pre', { textContent: d.failure_output })]);
  return s;
}

function discardedSection(d, where) {
  const discarded = d.discarded || [];
  if (!discarded.length) return null;
  const frozen = new Set(d.discarded_frozen || []);
  const s = section('discarded', 'Discarded changes', true, `${where}/record.json`);
  fill(s, plural(discarded.length, 'path'), JSON.stringify([discarded, [...frozen]]), () => [
    el('p', { textContent: 'Changes outside the scope (not editable, or frozen) were discarded:' }),
    el('ul', { className: 'paths' }, ...discarded.map((p) => el('li', {}, el('code', { textContent: p }),
      frozen.has(p) ? ' (frozen)' : '')))]);
  return s;
}

// Gates in list order: ✓ passed, ✗ failed, ○ not reached (pending); each with its stored output.
function gatesSection(d, where) {
  const gates = d.gates;
  const s = section('gates', 'Gates', false, `${where}/gates/`);
  const count = (v) => gates.filter((g) => g.passed === v).length;
  const words = [count(true) && `${count(true)} passed`, count(false) && `${count(false)} failed`,
    count(null) && `${count(null)} pending`].filter(Boolean).join(' · ');
  fill(s, words, JSON.stringify([gates, d.failure_output]), () => gates.map((g, index) => {
    const outcome = g.passed === true ? 'passed' : g.passed === false ? 'failed' : 'not-run';
    const facts = [
      g.kind === 'metric' && isNumber(g.value) && `${fmtNumber(g.value)}${isNumber(g.reference) ? ` vs ${fmtNumber(g.reference)}` : ''}`,
      isNumber(g.duration_s) && fmtDuration(g.duration_s),
      g.passed === null && 'pending',
    ].filter(Boolean).join(' · ');
    return el('div', { className: `gate ${outcome}` },
      el('div', {}, el('span', { className: 'gate-mark', textContent: { passed: '✓', failed: '✗', 'not-run': '○' }[outcome] }),
        el('code', { textContent: g.text }), facts ? el('span', { className: 'muted', textContent: `  ${facts}` }) : null),
      g.passed === false && g.reason ? el('p', { className: 'gate-reason', textContent: g.reason }) : null,
      // The failing command's output tail ("Why rejected" shows it too when it is the same).
      g.passed === false && g.output && g.output !== d.failure_output ? el('pre', { textContent: g.output }) : null,
      storedEl(d.iteration, 'gates', index + 1, g.stored));
  }));
  return s;
}

// Notes are written at the end of the agent's turn, before checks decide the outcome.
function noteSection(d, live, where) {
  const s = section('note', 'Agent note', false, `${where}/notes/iter-${d.iteration}.md`);
  s.body.className = 'sec-body prose';
  setHTML(s.body, (live ? '<p class="muted">Written before checks ran.</p>' : '') + d.notes);
  return s;
}

// This attempt's change: its own diff (start commit to frozen tree).
function attemptSection(d) {
  const s = section('attempt', 'Changes in this iteration');
  const a = d.attempt;
  s.node.firstChild.title = `git diff ${a.base.slice(0, 12)} ${a.tree.slice(0, 12)}${a.rejected_ref ? ` (kept as ${a.rejected_ref})` : ''}`;
  const diff = panel.diff;
  const text = diff?.text;
  const message = text ? null : diff?.error || (!diff || diff.loading ? 'Loading…' : 'No changes.');
  diffInto(s, 'attempt', text, message, false);
  return s;
}

// The code against the run's start: an accepted iteration's, or the edits so far while it runs.
function baselineDiffSection(d, live) {
  const s = section('changes', 'Changes vs baseline');
  diffInto(s, 'changes', d.diff, null, live);
  return s;
}

function diffInto(s, key, text, message, live) {
  const files = text ? parseDiff(text) : [];
  const add = files.reduce((sum, f) => sum + f.add, 0), del = files.reduce((sum, f) => sum + f.del, 0);
  const count = [files.length && `${plural(files.length, 'file')} · +${add} −${del}`, live && 'in progress',
    text?.includes('[… diff cut') && 'cut'].filter(Boolean).join(' · ');
  fill(s, count, JSON.stringify([text, message]), () => [
    ...(files.length === 1 ? [diffEl(files[0])] : files.map((f) => diffFileEl(f, key))),
    !files.length && el('p', { className: 'muted', textContent: message || 'No changes.' })]);
}

// One observe entry: its status and metrics, output (a review's as Markdown), files.
function observeSection(d, o, where) {
  const s = section(`observe:${o.name}`, o.name, false, `${where}/observe/${o.name}/`);
  const failed = o.ok === false;
  s.node.classList.toggle('failed', failed);
  const metrics = Object.entries(o.metrics || {}).map(([k, v]) => `${k} ${fmtNumber(v)}`).join(' · ');
  fill(s, [o.review && 'review', failed && `failed${o.error ? `: ${o.error}` : ''}`,
    // While the iteration runs, entries with output have finished; their status comes with the record.
    o.running && 'running', metrics, o.files.length && plural(o.files.length, 'file')].filter(Boolean).join(' · '),
  JSON.stringify(o), () => {
    const parts = [];
    if (failed) parts.push(el('p', { className: 'reason', textContent: `Failed${o.error ? `: ${o.error}` : ''}; its metrics are missing.` }));
    if (o.html) {
      const prose = el('div', { className: 'prose' });
      prose.innerHTML = o.html; // rendered by the server from Markdown with raw HTML disabled
      parts.push(prose);
    } else if (o.stdout) {
      parts.push(el('pre', { className: 'output', textContent: o.stdout }));
    } else if (o.stdout === null && SNAPSHOT) {
      parts.push(el('p', { className: 'muted', textContent: 'Output is not saved in this file; see autohelix view.' }));
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

// A stop command that failed (neither 0 nor 1, or timed out): its condition was never checked.
function stopProblem(x) {
  if (x.outcome !== 'error') return null;
  return x.timed_out ? 'timed out' : x.return_code === -1 ? 'could not run' : `exited ${x.return_code}`;
}

// The stop commands that ran after this iteration (accepted ones only): ■ the one that
// stopped the run, ✗ one that failed.
function stopSection(d, where) {
  const s = section('stop', 'Stop checks', false, `${where}/stop/`);
  const stops = d.stop;
  const failed = stops.filter(stopProblem).length;
  const words = [`${stops.length} ran`, failed && `${failed} failed`, stops.some((x) => x.stopped) && 'stopped the run']
    .filter(Boolean).join(' · ');
  fill(s, words, JSON.stringify(stops),
    () => stops.map((x) => el('div', { className: `gate ${x.stopped ? 'stopped' : stopProblem(x) ? 'failed' : 'passed'}` },
      el('div', {}, el('span', { className: 'gate-mark', textContent: x.stopped ? '■' : stopProblem(x) ? '✗' : '·' }),
        el('code', { textContent: x.command || `stop/${x.position}` }),
        x.stopped ? el('span', { className: 'muted', textContent: '  stopped the run' })
          : stopProblem(x) ? el('span', { className: 'muted', textContent: `  ${stopProblem(x)}: an error, not a condition` }) : null),
      storedEl(d.iteration, 'stop', x.position, x.stored))));
  return s;
}

// The agent's and judges' calls, live while they run.
function activitySection(d, where) {
  const calls = (d.calls || []).filter((call) => call.events.length > 0);
  if (!calls.length) return null;
  const s = section('activity', 'Call activity', false,
    `${where}/: agent.log; ask*.log, review*.log in gates/<k>/, observe/<name>/, stop/<k>/`);
  if (s.body.childElementCount !== calls.length) {
    s.body.replaceChildren(...calls.map(() => el('div', {}, el('h4'), el('div', { className: 'log' }))));
  }
  calls.forEach((call, index) => {
    const [heading, log] = s.body.children[index].children;
    setText(heading, `Call ${index + 1} · ${call.role}`);
    const key = call.log || call.live_log || `${call.role}-${index}`;
    if (!panel.expanded.has(key)) panel.expanded.set(key, new Set());
    const eventsKey = JSON.stringify(call.events);
    if (log.dataset.key !== eventsKey) { log.dataset.key = eventsKey; renderEvents(log, call.events, panel.expanded.get(key)); }
  });
  return s;
}

// A gate's or stop command's stored files (loaded when opened) and its ask/review transcripts.
function storedEl(iteration, kind, k, stored) {
  if (!stored) return null;
  // command.txt is the command the row names already.
  const items = stored.files.filter((f) => f.name !== 'command.txt').map((f) => fileEl(
    api(`/api/output/${iteration}/${kind}/${k}/${encodeURIComponent(f.name)}`), f.name, f.size,
    { id: `${kind}/${k}/${f.name}`, headTail: true }));
  for (const t of stored.transcripts) {
    const id = `${kind}/${k}/${t.name}`;
    const log = el('div', { className: 'log' });
    if (!panel.expanded.has(id)) panel.expanded.set(id, new Set());
    renderEvents(log, t.events, panel.expanded.get(id));
    items.push(fileLike(t.name + (t.events.length ? ` · ${plural(t.events.length, 'event')}` : ''), log, id));
  }
  return items.length ? el('div', { className: 'stored', title: `.autohelix/${iterDir(iteration)}/${stored.folder}/` }, ...items) : null;
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

// Of several files, each folds on its own (`path +a −d`, folded first); kept by section and path.
function diffFileEl(f, section) {
  const id = `${section}:${f.path}`;
  const item = el('details', { className: 'diff-file', open: panel.diffOpen.get(id) ?? false },
    el('summary', {}, el('code', { textContent: f.path }), el('span', { className: 'muted', textContent: ` +${f.add} −${f.del}` })),
    diffEl(f));
  item.addEventListener('toggle', () => panel.diffOpen.set(id, item.open));
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


// "bench (speed ↑)", as the console header lists observe entries.
function observeLabel(o) {
  const metrics = Object.entries(o.metrics || {}).map(([k, dir]) => `${k} ${dir === 'lower' ? '↓' : '↑'}`);
  return metrics.length ? `${o.name} (${metrics.join(', ')})` : o.name;
}

function renderConfig() {
  const c = state.config;
  show('config', !!c?.text);
  if (!c?.text) return;
  // Budgets are not config: the latest segment's flags, as its status records them.
  // Iterations and cost as run totals (spent before it plus its own cap); --time only
  // while the segment runs, against its clock (run_table.time_cap; a stop reason says
  // when time ended it).
  const s = state.status || {};
  const elapsed = s.state === 'running' && isNumber(s.segment_started_at)
    ? Math.max(0, Math.floor(Date.now() / 1000 - s.segment_started_at)) : null;
  const budget = [
    s.max_iterations && `to iteration ${s.max_iterations}`,
    s.max_cost_usd && `${fmtCap(s.max_cost_usd)} total`,
    s.max_time_seconds && elapsed !== null
      && `${fmtLimit(elapsed)} of ${fmtLimit(s.max_time_seconds)} (this segment)${elapsed >= s.max_time_seconds ? ' (time up)' : ''}`,
  ].filter(Boolean).join(' · ');
  const parts = [
    c.agent && ['Agent', c.agent],
    c.gates?.length && ['Gates', c.gates.join(' · '), c.gates.join('\n')],
    c.observe?.length && ['Observe', c.observe.map(observeLabel).join(' · ')],
    c.scope?.length && ['Editable', c.scope.join(', ')],
    budget && ['Budget', budget, `${budget}: the latest run/resume's -n/--until and --cost as run totals, its --time as its own`],
  ].filter(Boolean);
  const summary = $('config-summary');
  summary.title = `Show the full ${c.file}`;
  rebuild(summary, JSON.stringify([c.file, parts]), () => [el('b', { textContent: 'Config' }),
    el('span', { className: 'config-file', textContent: c.file }),
    // One line; long values are cut with "…" (hover, or open the config, for the full text).
    ...parts.map(([k, v, full]) => el('span', { className: 'chip', title: full || v }, el('b', { textContent: k + ' ' }), v))]);
  // The prompt template, stored with the run beside its config, after it.
  const pre = $('config-text');
  const t = c.template;
  const textKey = JSON.stringify([c.text, t || null]);
  if (pre.dataset.text !== textKey) {
    pre.dataset.text = textKey;
    pre.replaceChildren(...highlightYaml(c.text), ...(t ? [
      el('span', { className: 'yaml-comment', textContent: `\n# prompt_template ${t.file}${t.text === null ? ': unreadable' : ''}\n` }),
      t.text || '',
    ] : []));
  }
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
  setText('goal', (state.goal || '').trim());
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
  renderStats();
  renderMetricSelect(names);
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
    $('status').className = 'status crashed';
    if (!SNAPSHOT) setTimeout(loop, POLL_MS);
    return;
  }
  try {
    // Follow the run: the running iteration (or the summary) until the user picks another.
    const target = defaultTarget();
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
$('goal').title = 'Click to show the full goal';
$('goal').addEventListener('click', () => $('goal').classList.toggle('expanded'));
$('prev').addEventListener('click', () => step(-1));
$('next').addEventListener('click', () => step(1));
document.addEventListener('keydown', (e) => {
  if (!state || ['SELECT', 'INPUT', 'TEXTAREA'].includes(e.target.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === 'j') step(1); // down (newer)
  if (e.key === 'k') step(-1); // up (older)
});
const linked = location.hash.match(/^#iter-(\d+)$/);
if (linked) { selected = Number(linked[1]); followLive = false; panel = newPanel(selected); }
if (location.hash === '#summary') { selected = REPORT; followLive = false; panel = newPanel(REPORT); }
loop();
