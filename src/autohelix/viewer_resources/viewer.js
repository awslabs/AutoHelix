// AutoHelix live viewer: a browser for a run's .autohelix/ state. Polls /api/state
// and the details of whichever iteration cards are open. In report.html the same
// routes come from data built into the file, and nothing is polled.
'use strict';

const POLL_MS = 2000;
const SNAPSHOT = window.AUTOHELIX_SNAPSHOT || null;
const $ = (id) => document.getElementById(id);
// On /run/<id>/ the routes are /api/run/<id>/...; the snapshot keys them as /api/....
const RUN_PAGE = SNAPSHOT ? null : location.pathname.match(/^\/run\/([a-z0-9][a-z0-9-]*)\/$/);
const api = (route) => (RUN_PAGE ? route.replace(/^\/api\//, `/api/run/${RUN_PAGE[1]}/`) : route);

let state = null;
let selected = null;   // iteration whose card was opened last
let followLive = true; // keep selecting the running iteration until the user picks another
let metric = null;

// ---------- small helpers ----------

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

function fmtNumber(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
  return Math.abs(v) >= 100 ? v.toFixed(1) : Number(v.toPrecision(4)).toString();
}

function fmtDuration(seconds) {
  if (!seconds && seconds !== 0) return '—';
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
}

function fmtClock(epochSeconds) {
  const t = new Date(epochSeconds * 1000);
  const time = t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return t.toDateString() === new Date().toDateString()
    ? time : `${t.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${time}`;
}

function fmtCost(v) {
  return typeof v === 'number' ? `$${v.toFixed(2)}` : '—';
}

function label(n) {
  return n === 0 ? 'Baseline' : `Iteration ${n}`;
}

const better = (dir, a, b) => (dir === 'lower' ? a < b : a > b);

// Same rules as formatting.format_delta in the terminal summary.
function formatDelta(value, baseline) {
  if (typeof value !== 'number' || typeof baseline !== 'number' || baseline === 0 || value === baseline) return '';
  const pct = ((value - baseline) / baseline) * 100;
  const arrow = pct < 0 ? '↓' : '↑';
  const abs = Math.abs(pct);
  if (pct > 0 && abs >= 900) return `${arrow} ${(abs / 100 + 1).toFixed(0)}x`;
  if (pct < 0 && abs >= 90 && abs < 100 && 100 / (100 - abs) >= 10) return `${arrow} ${(100 / (100 - abs)).toFixed(0)}x`;
  return `${arrow} ${abs.toFixed(1)}%`;
}

function deltaEl(value, baseline, wrap = true) {
  const text = formatDelta(value, baseline);
  if (!text) return null;
  const dir = state.directions[metric];
  const cls = !dir ? '' : better(dir, value, baseline) ? 'better' : 'worse';
  return el('span', { className: `delta ${cls}`, textContent: wrap ? ` (${text})` : text });
}

function baselineValue() {
  return state.iterations.find((r) => r.iteration === 0)?.metrics?.[metric];
}

// ---------- status ----------

function liveIteration() {
  const s = state?.status;
  return s?.state === 'running' && s.phase !== 'report' && s.iteration !== null ? s.iteration : null;
}

function renderStatus() {
  const s = state.status;
  const node = $('status');
  if (!s) {
    node.textContent = 'No run recorded';
    node.className = 'status idle';
    return;
  }
  const now = Date.now() / 1000;
  if (s.state === 'running' && s.phase === 'report') {
    node.textContent = `Running · writing the summary · ${fmtDuration(now - s.phase_started_at)}`;
  } else if (s.state === 'running') {
    const where = s.iteration === null ? '' : ` · ${label(s.iteration).toLowerCase()}`;
    node.textContent = `Running${where} · ${s.phase} · ${fmtDuration(now - s.phase_started_at)}`;
  } else {
    const when = s.stopped_at ? ` at ${fmtClock(s.stopped_at)}` : '';
    const words = { finished: 'Finished', interrupted: 'Interrupted', failed: 'Failed', crashed: 'Stopped unexpectedly' };
    const why = s.state === 'finished' && s.stop_reason ? ` · ${s.stop_reason.detail}` : '';
    node.textContent = (words[s.state] || s.state) + when + why;
  }
  node.className = `status ${s.state}`;
}

// ---------- summary ----------

function renderReport() {
  const r = state.report;
  show('report', Boolean(r));
  if (!r) return;
  const body = $('report-body');
  if (body.dataset.html !== r.html) {
    body.dataset.html = r.html;
    body.innerHTML = r.html; // rendered by the server from Markdown with raw HTML disabled
    linkIterations(body);
  }
  setText('report-meta', [
    r.last_iteration && `iterations 1–${r.last_iteration}`,
    r.written_at && `written ${fmtClock(r.written_at)}`,
    r.seconds && fmtDuration(r.seconds),
    r.cost_usd && fmtCost(r.cost_usd),
  ].filter(Boolean).join(' · '));
  // Open a new summary, or the summary when the run finishes; fold it while a run is going.
  // In between, the user's fold wins.
  const running = state.status?.state === 'running';
  const box = $('report');
  const key = `${r.written_at}:${running}`;
  if (box.dataset.key !== key) {
    box.dataset.key = key;
    box.open = !running;
  }
}

// "iter N" in the summary links to that iteration's card.
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

// ---------- overview ----------

function metricNames() {
  const names = new Set(Object.keys(state.directions || {}));
  for (const r of state.iterations) Object.keys(r.metrics || {}).forEach((k) => names.add(k));
  return [...names];
}

function bestSoFar() {
  // Running best over accepted iterations, used for both the chart and deltas.
  const dir = state.directions[metric] || 'higher';
  let best = null;
  const out = new Map();
  for (const r of state.iterations) {
    const v = r.metrics?.[metric];
    if (r.accepted && typeof v === 'number' && (best === null || better(dir, v, best))) best = v;
    out.set(r.iteration, best);
  }
  return out;
}

function renderStats() {
  // Best metric, then run totals (as in the terminal's run summary): time over finished
  // iterations; cost also includes the baseline's calls.
  const its = state.iterations.filter((r) => r.iteration > 0);
  const cost = state.cost;
  const stats = [];
  if (metric) {
    const best = [...bestSoFar().values()].at(-1);
    stats.push([`Best ${metric}`, fmtNumber(best), deltaEl(best, baselineValue())]);
  }
  if (its.length) {
    const timed = its.filter((r) => typeof r.seconds === 'number');
    stats.push(['Time', timed.length
      ? fmtDuration(timed.reduce((sum, r) => sum + r.seconds, 0)) + (timed.length < its.length ? ' (incomplete)' : '')
      : '—']);
  }
  if (its.length || typeof cost?.cost_usd === 'number' || cost?.incomplete) {
    stats.push(['Cost', typeof cost?.cost_usd === 'number'
      ? fmtCost(cost.cost_usd) + (cost.incomplete ? ' (incomplete)' : '')
      : 'unavailable']);
  }
  $('stats').replaceChildren(...stats.map(([k, v, extra]) => el('div', { className: 'stat' },
    el('span', { className: 'stat-label', textContent: k }),
    el('span', { className: 'stat-value', textContent: v }, extra))));
}

function renderMetricSelect(names) {
  const select = $('metric'); // only shown when there is more than one metric
  select.hidden = names.length < 2;
  if (JSON.stringify(Array.from(select.options, (option) => option.value)) !== JSON.stringify(names)) {
    select.replaceChildren(...names.map((n) => el('option', { value: n, textContent: n })));
  }
  select.value = metric;
  $('metric-col').textContent = metric || '';
}

function renderChart() {
  const svg = $('chart');
  $('chart-box').hidden = !metric; // runs without metrics have no chart (`hidden` does not apply to <svg>)
  if (!metric) return;
  const W = 640, H = 200, pad = { l: 52, r: 12, t: 10, b: 22 };
  const points = state.iterations.filter((r) => typeof r.metrics?.[metric] === 'number');
  const values = points.map((r) => r.metrics[metric]);
  if (!values.length) { svg.replaceChildren(); return; }
  const best = bestSoFar();
  const n = Math.max(1, state.iterations.at(-1).iteration, liveIteration() ?? 0);
  const lo = Math.min(...values), hi = Math.max(...values);
  const log = lo > 0 && hi / lo > 20; // big speedups are unreadable on a linear axis
  const ticks = log ? logTicks(lo, hi) : linearTicks(lo, hi);
  const [y0, y1] = [ticks[0], ticks.at(-1)];
  const f = (v) => (log ? Math.log10(v) : v);
  const x = (i) => pad.l + (i / n) * (W - pad.l - pad.r);
  const y = (v) => (y0 === y1 ? H / 2 : pad.t + (1 - (f(v) - f(y0)) / (f(y1) - f(y0))) * (H - pad.t - pad.b));
  const svgEl = (tag, attrs, text) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const children = [];
  for (const v of ticks) {
    children.push(svgEl('line', { x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v), class: 'grid' }));
    children.push(svgEl('text', { x: pad.l - 6, y: y(v) + 4, 'text-anchor': 'end', class: 'axis' }, fmtTick(v, log ? null : ticks[1] - ticks[0])));
  }
  const dir = state.directions[metric];
  setText('scale', [dir && `${dir} is better`, log && 'log scale'].filter(Boolean).join(' · '));
  const xTicks = Math.min(n, 10);
  for (let k = 0; k <= xTicks; k++) {
    const i = Math.round((k / xTicks) * n);
    children.push(svgEl('text', { x: x(i), y: H - 4, 'text-anchor': 'middle', class: 'axis' }, String(i)));
  }
  // Step line: best accepted value so far.
  let d = '';
  for (const r of state.iterations) {
    const b = best.get(r.iteration);
    if (b === null) continue;
    d += d ? ` H${x(r.iteration)} V${y(b)}` : `M${x(r.iteration)} ${y(b)}`;
  }
  if (d && liveIteration() !== null) d += ` H${x(n)}`;
  children.push(svgEl('path', { d, class: 'best-line' }));
  for (const r of points) {
    const dot = svgEl('circle', {
      cx: x(r.iteration), cy: y(r.metrics[metric]), r: r.iteration === selected ? 6 : 4,
      class: `dot ${r.accepted ? 'accepted' : 'rejected'}${r.iteration === selected ? ' selected' : ''}`,
    });
    dot.append(svgEl('title', {}, `${label(r.iteration)}: ${fmtNumber(r.metrics[metric])}`));
    dot.addEventListener('click', () => select(r.iteration));
    children.push(dot);
  }
  svg.replaceChildren(...children);
}

// Axis ticks: powers of ten on a log axis, round steps on a linear one.
function logTicks(lo, hi) {
  const ticks = [];
  for (let k = Math.floor(Math.log10(lo)); k <= Math.ceil(Math.log10(hi)); k++) ticks.push(10 ** k);
  return ticks;
}

function linearTicks(lo, hi) {
  if (lo === hi) return [lo];
  const raw = (hi - lo) / 4;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const ticks = [];
  for (let v = Math.floor(lo / step) * step; v <= hi + step * 1e-9; v += step) ticks.push(Number(v.toPrecision(12)));
  if (ticks.at(-1) < hi) ticks.push(ticks.at(-1) + step);
  return ticks;
}

const compact = new Intl.NumberFormat(undefined, { notation: 'compact', maximumSignificantDigits: 3 });
// Labels get as many decimals as the tick step needs, so close ticks stay distinct.
function fmtTick(v, step) {
  if (Math.abs(v) >= 1000 && (!step || step >= 100)) return compact.format(v);
  if (!step) return String(Number(v.toPrecision(3)));
  const decimals = Math.max(0, Math.ceil(-Math.log10(step) - 1e-9) + (String(Number((step / 10 ** Math.floor(Math.log10(step))).toPrecision(6))).includes('.') ? 1 : 0));
  return v.toFixed(Math.min(decimals, 10));
}

function renderRows() {
  const base = baselineValue();
  const rows = state.iterations.map((r) => {
    const v = r.metrics?.[metric];
    const outcome = r.iteration === 0 ? 'baseline' : r.accepted ? 'accepted' : 'rejected';
    return rowEl(r.iteration, [
      el('td', { textContent: r.iteration === 0 ? '0' : String(r.iteration) }),
      el('td', {}, el('span', { className: `pill ${outcome}`, textContent: outcome })),
      el('td', { className: 'value' }, metric ? fmtNumber(v) : '', metric && r.iteration > 0 ? deltaEl(v, base) : null),
      summaryCell(r),
      el('td', { className: 'num', textContent: r.seconds ? fmtDuration(r.seconds) : '' }),
      el('td', { className: 'num', textContent: fmtCost(r.cost) + (typeof r.cost === 'number' && r.cost_incomplete ? ' (incomplete)' : ''),
        title: r.cost_incomplete ? 'Some call costs are unavailable' : '' }),
    ]);
  });
  const live = liveIteration();
  if (live !== null && !state.iterations.some((r) => r.iteration === live)) {
    rows.push(rowEl(live, [
      el('td', { textContent: String(live) }),
      el('td', {}, el('span', { className: 'pill running', textContent: 'running' })),
      el('td'), el('td', { className: 'summary muted', textContent: phaseText(state.status.phase) }), el('td'), el('td'),
    ]));
  }
  $('rows').replaceChildren(...rows);
}

// Rejected rows: why (the failing gate), then the agent's one-line summary under it.
// Observe failures and discarded changes are flagged; the details are in the card.
function summaryCell(r) {
  const why = r.accepted === false ? rejection(r) : '';
  const summary = r.summary && r.summary !== r.reason ? r.summary.split('\n')[0] : '';
  const failed = (r.observe || []).filter((o) => o.ok === false).map((o) => o.name);
  const flags = [
    failed.length && `${failed.join(', ')} failed`,
    r.discarded?.length && `${r.discarded.length} out-of-scope change${r.discarded.length === 1 ? '' : 's'} discarded`,
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

const PHASES = {
  starting: 'Starting…', baseline: 'Measuring the baseline…', agent: 'Agent is working…',
  checks: 'Running gates…', benchmark: 'Running observe entries…',
  merge: 'Merging the accepted change…', 'between iterations': 'Wrapping up…',
};
const phaseText = (phase) => PHASES[phase] || `${phase}…`;

function rowEl(iteration, cells) {
  const tr = el('tr', { tabIndex: 0, className: iteration === selected ? 'selected' : '' }, ...cells);
  tr.addEventListener('click', () => select(iteration));
  tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') select(iteration); });
  return tr;
}

// ---------- iteration cards ----------
// One foldable card per iteration (notes, activity, changes, output), in table order.
// Selecting an iteration (row, chart point, j/k) opens its card and closes the others.

const cards = new Map(); // iteration -> card

function iterationOrder() {
  const order = state.iterations.map((r) => r.iteration);
  const live = liveIteration();
  if (live !== null && !order.includes(live)) order.push(live);
  return order;
}

function select(iteration) {
  if (iteration === undefined || !state) return;
  followLive = iteration === liveIteration();
  openOnly(iteration);
  history.replaceState(null, '', followLive ? location.pathname : `#iter-${iteration}`);
  render();
  cards.get(iteration)?.node.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  refreshCards();
}

function openOnly(iteration) {
  selected = iteration;
  for (const [i, card] of cards) card.node.open = i === iteration;
}

function setText(id, text) {
  const node = typeof id === 'string' ? $(id) : id;
  if (node.textContent !== text) node.textContent = text;
}

function show(id, visible) { (typeof id === 'string' ? $(id) : id).hidden = !visible; }

// Only for HTML the server rendered from Markdown with raw HTML disabled.
function setHTML(node, html) {
  if (node.dataset.html !== html) { node.dataset.html = html; node.innerHTML = html; }
}

function section(title, source, open = false) {
  const count = el('span', { className: 'muted' });
  const body = el('div');
  const head = el('summary', { title: source ? `.autohelix/${source}` : '' }, title + ' ', count);
  const node = el('details', { className: 'sec', open, hidden: true }, head, body);
  return { node, count, body };
}

function cardEl(i) {
  const summary = el('summary');
  const said = el('p', { className: 'agent-summary', hidden: true });
  const failure = section('Why rejected', 'history.jsonl');
  const discarded = section('Discarded changes', 'history.jsonl', true);
  const gates = section('Gates', 'history.jsonl');
  const notes = section('Agent note', `notes/iter-${i}.md`);
  const attempt = section('Changes in this iteration', null);
  const changes = section('Changes vs baseline', null);
  const observe = el('div', { className: 'observe' }); // one section per observe entry
  const activity = section('Agent activity', `logs/iter-${i}/agent.log`);
  const phases = section('Call activity', `logs/iter-${i}/call-*/`);
  notes.body.className = 'prose';
  activity.body.className = 'log';
  attempt.body.className = changes.body.className = 'diff';
  gates.body.className = 'gates';
  discarded.body.className = 'entry';
  const node = el('details', { className: 'box card', open: i === selected }, summary,
    // Outcome first, then the agent's account, the evidence, and raw logs last.
    el('div', { className: 'card-body' }, said, failure.node, discarded.node, gates.node, notes.node,
      attempt.node, changes.node, observe, activity.node, phases.node));
  const card = { i, node, summary, said, failure, discarded, gates, notes, attempt, changes, observe, activity, phases,
                 key: undefined, attemptKey: undefined, opened: new Set(), expanded: { activity: new Set() } };
  attempt.node.addEventListener('toggle', () => attempt.node.open && loadAttempt(card));
  node.addEventListener('toggle', () => {
    if (!node.open) return;
    if (selected !== i) { selected = i; followLive = i === liveIteration(); render(); }
    refreshCards();
  });
  return card;
}

function renderCards() {
  const box = $('cards');
  const order = iterationOrder();
  for (const i of order) {
    if (!cards.has(i)) cards.set(i, cardEl(i));
    const card = cards.get(i);
    const r = state.iterations.find((x) => x.iteration === i);
    const live = i === liveIteration() && !r;
    const outcome = live ? 'running' : i === 0 ? 'baseline' : r.accepted ? 'accepted' : 'rejected';
    // Title shows only the outcome; metric values live in the table and chart.
    const why = outcome === 'rejected' ? rejection(r) : '';
    const key = JSON.stringify([outcome, why, live && state.status.phase]);
    if (card.summary.dataset.key !== key) {
      card.summary.dataset.key = key;
      card.summary.replaceChildren(...[
        el('span', { className: 'card-title', textContent: label(i) }),
        i > 0 ? el('span', { className: `pill ${outcome}`, textContent: outcome }) : null,
        live ? el('span', { className: 'muted', textContent: phaseText(state.status.phase) }) : null,
        why ? el('span', { className: 'muted', textContent: why }) : null,
      ].filter(Boolean));
    }
  }
  for (const [i, card] of cards) if (!order.includes(i)) { card.node.remove(); cards.delete(i); }
  const wanted = order.map((i) => cards.get(i).node);
  if (wanted.some((node, k) => box.children[k] !== node)) box.replaceChildren(...wanted);
  renderSharedNotes();
}

// Harness rejection reasons, shortened for a card title (full text is in "Why rejected").
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

function renderSharedNotes() {
  // Notes that belong to the whole run rather than one iteration (e.g. ideas.md).
  const files = (state.notes_files || []).filter((name) => !/^iter-\d+\.md$/.test(name));
  const box = $('shared-notes');
  show(box, files.length > 0);
  setText('shared-count', files.length ? `· ${files.length} file${files.length === 1 ? '' : 's'}` : '');
  const list = $('shared-list');
  const key = JSON.stringify(files);
  if (list.dataset.key === key) return;
  list.dataset.key = key;
  list.replaceChildren(...files.map((name) => {
    const body = el('div', { className: 'prose' });
    const item = el('details', { className: 'note' }, el('summary', { textContent: name }), body);
    item.addEventListener('toggle', () => item.open && loadNote(name, body));
    return item;
  }));
}

async function loadNote(name, body) {
  try { body.innerHTML = (await getJSON(`/api/notes/${encodeURIComponent(name)}`)).html || ''; } // raw HTML disabled server-side
  catch { body.textContent = 'Could not load this note.'; }
}

async function refreshCards() {
  // Only open cards are loaded. Finished iterations change only when history or notes
  // do (observations are saved before the record); the running one changes constantly.
  for (const [i, card] of cards) {
    if (!card.node.open) continue;
    const r = state.iterations.find((x) => x.iteration === i);
    const key = i === liveIteration() ? null
      : JSON.stringify([r, state.notes_files.includes(`iter-${i}.md`)]);
    if (key !== null && key === card.key) continue;
    try {
      const d = await getJSON(`/api/iteration/${i}`);
      card.key = key;
      fillCard(card, d);
    } catch { /* the next poll retries */ }
  }
}

function fillCard(card, d) {
  const live = d.iteration === liveIteration() && !state.iterations.some((x) => x.iteration === d.iteration);
  const row = state.iterations.find((x) => x.iteration === d.iteration && x.accepted === false);
  const reason = row && (row.reason || row.summary);
  setText(card.said, d.summary || '');
  show(card.said, !!d.summary);
  show(card.failure.node, !!(reason || d.failure_output));
  card.failure.body.replaceChildren(...[
    reason ? el('p', { className: 'reason', textContent: reason }) : null,
    d.failure_output ? el('pre', { textContent: d.failure_output }) : null,
  ].filter(Boolean));

  const discarded = d.discarded || [];
  show(card.discarded.node, discarded.length > 0);
  setText(card.discarded.count, discarded.length ? `· ${discarded.length} path${discarded.length === 1 ? '' : 's'}` : '');
  card.discarded.body.replaceChildren(...(discarded.length ? [
    el('p', { className: 'warn', textContent: 'Changes outside the editable scope were discarded:' }),
    el('ul', { className: 'paths' }, ...discarded.map((p) => el('li', {}, el('code', { textContent: p })))),
  ] : []));

  fillGates(card, d.gates || [], d.failure_output);

  const attemptKey = d.attempt ? `${d.attempt.base}:${d.attempt.tree}` : null;
  show(card.attempt.node, !!d.attempt);
  if (attemptKey !== card.attemptKey) {
    card.attemptKey = attemptKey;
    card.attempt.loaded = null;
    card.attempt.body.replaceChildren();
    setText(card.attempt.count, '');
    if (card.attempt.node.open) loadAttempt(card);
  }
  card.attempt.node.querySelector('summary').title = d.attempt
    ? `git diff ${d.attempt.base.slice(0, 12)} ${d.attempt.tree.slice(0, 12)}`
      + (d.attempt.rejected_ref ? ` (kept as ${d.attempt.rejected_ref})` : '') : '';

  fillObserve(card, d);

  // Notes are written at the end of the agent's turn, before checks decide the outcome.
  show(card.notes.node, !!d.notes);
  setHTML(card.notes.body, (live ? '<p class="muted">Written before checks ran.</p>' : '') + (d.notes || ''));

  const shown = steps(d.agent);
  const calls = shown.filter((x) => x.type === 'tool').length;
  const says = shown.filter((x) => x.type === 'say').length;
  show(card.activity.node, shown.length > 0);
  setText(card.activity.count, [
    calls && `${calls} tool call${calls === 1 ? '' : 's'}`,
    says && `${says} message${says === 1 ? '' : 's'}`,
    d.agent_clipped && 'earliest activity not shown',
  ].filter(Boolean).map((x) => `· ${x}`).join(' '));
  renderEvents(card.activity.body, d.agent, card.expanded.activity);

  show(card.changes.node, !!d.diff);
  const diffKey = JSON.stringify([d.diff, live]);
  if (card.changes.body.dataset.key !== diffKey) {
    card.changes.body.dataset.key = diffKey;
    renderDiff(card.changes.body, card.changes.count, d.diff || '', live);
  }

  const phases = (d.calls || []).filter((call) => call.events.length > 0);
  show(card.phases.node, phases.length > 0);
  if (phases.length) {
    show(card.activity.node, false);
    card.phases.body.replaceChildren(...phases.map((call, index) => {
      const heading = el('h4', { textContent: `Call ${index + 1} · ${call.role}` });
      const log = el('div', { className: 'log' });
      card.expanded.phases ||= new Map();
      const key = call.log || call.id || `${call.role}-${index}`;
      if (!card.expanded.phases.has(key)) card.expanded.phases.set(key, new Set());
      renderEvents(log, call.events, card.expanded.phases.get(key));
      return el('div', {}, heading, log);
    }));
  }
}

// Gates in list order: ✓ passed, ✗ failed, ○ not reached (pending).
function fillGates(card, gates, failureOutput) {
  show(card.gates.node, gates.length > 0);
  const count = (v) => gates.filter((g) => g.passed === v).length;
  setText(card.gates.count, gates.length ? '· ' + [
    count(true) && `${count(true)} passed`, count(false) && `${count(false)} failed`, count(null) && `${count(null)} pending`,
  ].filter(Boolean).join(' · ') : '');
  const key = JSON.stringify([gates, failureOutput]);
  if (card.gatesKey === key) return;
  card.gatesKey = key; // a property, not an attribute: it holds output text
  card.gates.body.replaceChildren(...gates.map((g) => {
    const outcome = g.passed === true ? 'passed' : g.passed === false ? 'failed' : 'pending';
    const facts = [
      g.kind === 'metric' && typeof g.value === 'number'
        ? `${fmtNumber(g.value)}${typeof g.reference === 'number' ? ` vs ${fmtNumber(g.reference)}` : ''}` : null,
      typeof g.duration_s === 'number' ? fmtDuration(g.duration_s) : null,
      g.passed === null ? 'pending' : null,
    ].filter(Boolean).join(' · ');
    return el('div', { className: `gate ${outcome}` },
      el('div', {},
        el('span', { className: 'mark', textContent: { passed: '✓', failed: '✗', pending: '○' }[outcome] }),
        el('code', { textContent: g.text }),
        facts ? el('span', { className: 'muted', textContent: `  ${facts}` }) : null),
      g.passed === false && g.reason ? el('p', { className: 'reason', textContent: g.reason }) : null,
      // The failing command's output tail ("Why rejected" shows it too when it is the same).
      g.passed === false && g.output && g.output !== failureOutput ? el('pre', { textContent: g.output }) : null);
  }));
}

// One section per observe entry, in the order they ran.
function fillObserve(card, d) {
  const key = JSON.stringify(d.observe);
  if (card.observeKey === key) return;
  card.observeKey = key;
  card.observe.replaceChildren(...(d.observe || []).map((o) => {
    const id = o.legacy ? ':legacy' : o.name;
    const s = section(o.legacy ? 'Captured output' : o.name,
      `runs/<id>/observations/iter-${d.iteration}/${o.legacy ? '' : o.name + '/'}`, card.opened.has(id));
    s.node.hidden = false;
    s.body.className = 'entry';
    s.node.addEventListener('toggle', () => (s.node.open ? card.opened.add(id) : card.opened.delete(id)));
    const metrics = Object.entries(o.metrics || {}).map(([k, v]) => `${k} ${fmtNumber(v)}`).join(' · ');
    const failed = o.ok === false;
    setText(s.count, [
      o.review ? 'review' : null,
      failed ? `failed${o.error ? `: ${o.error}` : ''}` : null,
      // While the iteration runs, entries with output have finished; their status comes with the record.
      o.running ? 'running' : null,
      metrics || null,
      o.files.length ? `${o.files.length} file${o.files.length === 1 ? '' : 's'}` : null,
    ].filter(Boolean).map((x) => `· ${x}`).join(' '));
    if (failed) s.node.classList.add('failed');
    const parts = [];
    if (failed) parts.push(el('p', { className: 'reason', textContent: `Failed${o.error ? `: ${o.error}` : ''}; its metrics are missing.` }));
    if (o.html) {
      const prose = el('div', { className: 'prose' });
      prose.innerHTML = o.html; // rendered by the server from Markdown with raw HTML disabled
      parts.push(prose);
    } else if (o.stdout) {
      parts.push(el('pre', { className: 'output', textContent: o.stdout }));
    } else if (!o.legacy && o.stdout === null && SNAPSHOT) {
      parts.push(el('p', { className: 'muted', textContent: 'Output is not saved in this file; see autohelix view --web.' }));
    }
    if (o.stderr) {
      parts.push(el('details', { className: 'artifact' }, el('summary', { textContent: 'stderr' }),
        el('div', { className: 'artifact-body' }, el('pre', { textContent: o.stderr }))));
    }
    if (o.files.length) {
      const prefix = o.legacy ? '' : `${o.name}/`;
      parts.push(el('div', { className: 'artifacts' }, ...o.files.map((a) => artifactEl(d.iteration, a, prefix))));
    }
    if (o.stdout_file && !SNAPSHOT) {
      parts.push(el('a', { className: 'raw', href: artifactUrl(d.iteration, o.stdout_file), target: '_blank', rel: 'noopener', textContent: 'stdout.txt' }));
    }
    s.body.replaceChildren(...parts);
    return s.node;
  }));
}


// The attempt's own change (start commit to frozen tree), loaded when its section opens.
async function loadAttempt(card) {
  const key = card.attemptKey;
  if (!key || card.attempt.loaded === key) return;
  card.attempt.loaded = key;
  let d;
  try { d = await getJSON(`/api/iteration/${card.i}/diff`); }
  catch {
    d = { diff: null, error: SNAPSHOT ? 'Not saved in this file; see autohelix view --web.' : 'Could not load the diff.' };
    if (!SNAPSHOT) card.attempt.loaded = null; // retry on the next open
  }
  if (card.attemptKey !== key) return;
  if (d.diff) renderDiff(card.attempt.body, card.attempt.count, d.diff, false);
  else {
    setText(card.attempt.count, '');
    card.attempt.body.replaceChildren(el('p', { className: 'muted', textContent: d.error || 'No changes.' }));
  }
}

function fmtSize(bytes) {
  return bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
}

// A file in observations/iter-N/, by its path there; each component encoded on its own.
function artifactUrl(iteration, path) {
  return api(`/api/artifact/${iteration}/${path.split('/').map(encodeURIComponent).join('/')}`);
}

function artifactEl(iteration, a, prefix = '') {
  const url = artifactUrl(iteration, a.path);
  const name = a.path.startsWith(prefix) ? a.path.slice(prefix.length) : a.path;
  const body = el('div', { className: 'artifact-body' });
  const item = el('details', { className: 'artifact' },
    el('summary', {}, el('span', { textContent: name }), el('span', { className: 'muted', textContent: ` · ${fmtSize(a.size)}` })),
    body);
  item.addEventListener('toggle', async () => {
    if (!item.open || body.childElementCount) return;
    if (/\.(png|jpe?g|gif|webp)$/i.test(name)) {
      body.append(el('img', { src: url, alt: name }));
    } else {
      const text = await (await fetch(url, { cache: 'no-store' })).text();
      body.append(el('pre', { textContent: text.length > 262144 ? text.slice(0, 262144) + '\n…' : text }));
    }
    body.append(el('a', { href: url, target: '_blank', rel: 'noopener', textContent: 'Open in new tab' }));
  });
  return item;
}

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
  $('config').hidden = !c?.text;
  if (!c?.text) return;
  // `run -n N` overrides the file for that run; the harness records what it used.
  const runLimit = state.status?.max_iterations;
  const iterations = runLimit && runLimit !== c.max_iterations
    ? `${runLimit} iterations (run -n ${runLimit}; file says ${c.max_iterations})`
    : c.max_iterations && `${c.max_iterations} iterations`;
  const budget = [
    iterations,
    c.max_cost_usd && `$${c.max_cost_usd}`,
    c.max_time_seconds && fmtDuration(c.max_time_seconds),
  ].filter(Boolean).join(', ');
  const parts = [
    c.agent && ['Agent', c.agent],
    c.gates?.length && ['Gates', c.gates.join(' · '), c.gates.join('\n')],
    c.observe?.length && ['Observe', c.observe.map(observeLabel).join(' · ')],
    c.scope?.length && ['Editable', c.scope.join(', ')],
    budget && ['Budget', budget],
  ].filter(Boolean);
  const summary = $('config-summary');
  summary.title = `Show the full ${c.file}`;
  const key = JSON.stringify(parts);
  if (summary.dataset.key !== key) {
    summary.dataset.key = key;
    summary.replaceChildren(el('b', { textContent: 'Config' }), el('span', { className: 'config-file', textContent: c.file }),
      // One line; long values are cut with "…" (hover, or open the config, for the full text).
      ...parts.map(([k, v, full]) => el('span', { className: 'chip', title: full || v }, el('b', { textContent: k + ' ' }), v)));
  }
  const pre = $('config-text');
  if (pre.dataset.text !== c.text) { pre.dataset.text = c.text; pre.replaceChildren(...highlightYaml(c.text)); }
}

function renderDiff(box, stat, text, live) {
  // One block per file with old/new line numbers; unchanged stretches between
  // changed regions are shown as "⋯ N unchanged lines" instead of git's @@ headers.
  const files = [];
  let file = null, oldNo = 0, newNo = 0, lastNew = 0;
  const row = (kind, a, b, sign, code) => el('div', { className: `row ${kind}` },
    el('span', { className: 'ln', textContent: a }), el('span', { className: 'ln', textContent: b }),
    el('span', { className: 'sign', textContent: sign }), el('span', { className: 'code', textContent: code }));
  let cut = '';
  for (const line of text.split('\n')) {
    if (line.startsWith('[… diff cut')) { // the server stopped reading there
      cut = line.replace(/^\[|\]$/g, '');
      file?.rows.push(el('div', { className: 'gap', textContent: cut }));
    } else if (line === '') {
      continue; // the end (empty context lines are " ")
    } else if (line.startsWith('diff --git ')) {
      file = { path: line.replace(/^diff --git .* b\/(.*)$/, '$1'), rows: [], add: 0, del: 0 };
      files.push(file);
      lastNew = 0;
    } else if (!file || /^(index |--- |\+\+\+ |new file mode|deleted file mode|similarity |rename |\\ No newline)/.test(line)) {
      continue;
    } else if (line.startsWith('@@')) {
      const m = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (!m) continue;
      [oldNo, newNo] = [Number(m[1]), Number(m[2])];
      const skipped = newNo - lastNew - 1;
      if (skipped > 0) file.rows.push(el('div', { className: 'gap', textContent: `⋯ ${skipped} unchanged line${skipped === 1 ? '' : 's'}` }));
    } else if (line[0] === '+') {
      file.add++; lastNew = newNo;
      file.rows.push(row('add', '', newNo++, '+', line.slice(1)));
    } else if (line[0] === '-') {
      file.del++;
      file.rows.push(row('del', oldNo++, '', '−', line.slice(1)));
    } else {
      lastNew = newNo;
      file.rows.push(row('ctx', oldNo++, newNo++, '', line.slice(1)));
    }
  }
  const total = files.reduce((n, f) => [n[0] + f.add, n[1] + f.del], [0, 0]);
  setText(stat, (files.length ? `· ${files.length} file${files.length === 1 ? '' : 's'} · +${total[0]} −${total[1]}` : '')
    + (live ? ' · in progress' : '') + (cut ? ' · cut' : ''));
  box.replaceChildren(...(files.length ? files.map((f) => el('div', { className: 'diff-file' },
    el('div', { className: 'diff-path' }, el('span', { textContent: f.path }),
      el('span', { className: 'muted', textContent: ` +${f.add} −${f.del}` })),
    el('pre', {}, ...f.rows))) : [el('pre', { textContent: text })]));
}

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
  const nodes = steps(events).map((step, i) => {
    if (step.html) {
      const node = el('div', { className: 'ev say prose' });
      node.innerHTML = step.html; // Server-rendered Markdown, raw HTML disabled.
      return node;
    }
    if (step.type === 'hidden-think') return el('div', { className: 'ev think', textContent: 'Thinking (not logged)' });
    if (step.type !== 'tool') return el('div', { className: `ev ${step.type}`, textContent: step.text });
    // Claude: name "Bash" + input; Codex: the name is the command itself.
    const command = shortCommand(step.input || step.name);
    const first = command.split('\n')[0];
    const label = step.input ? `${step.name}  ${first}` : first;
    const body = [command.includes('\n') ? command : null, step.result || null].filter(Boolean).join('\n\n');
    const node = el('details', { className: `ev tool${step.error ? ' error' : ''}`, open: expanded.has(i) },
      el('summary', { textContent: label.length > 160 ? label.slice(0, 160) + '…' : label }),
      body ? el('pre', { textContent: body.length > 4000 ? body.slice(0, 4000) + '\n…' : body }) : null);
    node.addEventListener('toggle', () => (node.open ? expanded.add(i) : expanded.delete(i)));
    return node;
  });
  box.replaceChildren(...nodes);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

// ---------- polling ----------

async function getJSON(url) {
  if (SNAPSHOT) {
    if (url in SNAPSHOT.routes) return SNAPSHOT.routes[url];
    throw new Error(`${url}: not in this file`);
  }
  const res = await fetch(api(url), { cache: 'no-store' });
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  return res.json();
}

function render() {
  const names = metricNames();
  if (!names.includes(metric)) metric = names[0] || null;
  // The page is about one run: the goal's title (its first line) heads it, the run id and
  // project sit above as the way back to the list. The rest of the goal is below the title.
  const project = state.project;
  const title = state.title || state.run_id || project;
  setText('title', title);
  document.title = `${title} · ${state.run_id || project} · AutoHelix${SNAPSHOT ? ' report' : ''}`;
  if (SNAPSHOT) {
    show('kind', true);
    setText('kind', [project, state.run_id, `run report · saved ${fmtClock(SNAPSHOT.saved_at)}`].filter(Boolean).join(' · '));
  } else if (state.run_id) { show('crumbs', true); setText('crumb-project', project); setText('crumb-run', state.run_id); }
  const goal = (state.goal || '').trim();
  const [first, ...rest] = goal.split('\n');
  setText('goal', state.title && first.trim() === state.title ? rest.join('\n').trim() : goal);
  renderStatus();
  renderConfig();
  renderReport();
  renderStats();
  renderMetricSelect(names);
  renderChart();
  renderRows();
  renderCards();
}

async function pollState() {
  try {
    state = await getJSON('/api/state');
  } catch {
    $('status').textContent = 'Viewer disconnected — retrying';
    $('status').className = 'status crashed';
    return;
  }
  // Follow the run: open the running (or latest) iteration until the user picks another.
  const target = liveIteration() ?? state.iterations.at(-1)?.iteration ?? null;
  if (selected === null || (followLive && target !== null && target !== selected)) selected = target;
  render(); // errors here are UI bugs, not a lost connection; loop() logs them
  openOnlyOnce();
}

let openedFor = undefined;
function openOnlyOnce() {
  // Open the selected card when the selection changes, without fighting the user's own folds.
  if (openedFor === selected) return;
  openedFor = selected;
  openOnly(selected);
}

async function loop() {
  try {
    await pollState();
    await refreshCards();
  } catch (err) {
    console.error(err); // a rendering bug; keep polling
  } finally {
    if (!SNAPSHOT) setTimeout(loop, document.hidden ? POLL_MS * 5 : POLL_MS);
  }
}

// Theme: Auto (follow the system) -> Light -> Dark, remembered in this browser if storage allows.
const THEMES = { auto: '◐ Auto', light: '☀ Light', dark: '☾ Dark' };
const systemDark = matchMedia('(prefers-color-scheme: dark)');
let theme = 'auto';
try { theme = localStorage.getItem('autohelix-theme') || 'auto'; } catch { /* storage blocked */ }
if (!(theme in THEMES)) theme = 'auto';

function applyTheme() {
  if (theme === 'auto') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  const dark = theme === 'dark' || (theme === 'auto' && systemDark.matches);
  const logos = SNAPSHOT?.logos || { light: '/logo-light.svg', dark: '/logo-dark.svg' };
  $('logo').src = dark ? logos.dark : logos.light;
  $('theme').textContent = THEMES[theme];
}
$('theme').addEventListener('click', () => {
  const order = Object.keys(THEMES);
  theme = order[(order.indexOf(theme) + 1) % order.length];
  try { localStorage.setItem('autohelix-theme', theme); } catch { /* still applies to this page */ }
  applyTheme();
});
systemDark.addEventListener('change', applyTheme);
applyTheme();

$('metric').addEventListener('change', (e) => { metric = e.target.value; render(); });
$('goal').title = 'Click to show the full goal';
$('goal').addEventListener('click', () => $('goal').classList.toggle('expanded'));
document.addEventListener('keydown', (e) => {
  if (!state || e.target.tagName === 'SELECT') return;
  const order = iterationOrder();
  const i = order.indexOf(selected);
  if (e.key === 'j') select(order[Math.min(order.length - 1, i + 1)]); // down (newer)
  if (e.key === 'k') select(order[Math.max(0, i - 1)]); // up (older)
});
const linked = location.hash.match(/^#iter-(\d+)$/);
if (linked) { selected = Number(linked[1]); followLive = false; }
loop();
