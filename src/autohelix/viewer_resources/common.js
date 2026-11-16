// AutoHelix web pages: what the run list (dashboard.js) and the run page (viewer.js)
// share. Numbers, changes, durations and money as the console writes them
// (terminal.number, terminal.change, formatting.format_duration, terminal.money).
// Loaded before the page's own script; report.html inlines it. Node loads it in the tests.
'use strict';

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  node.append(...children.filter((c) => c !== null && c !== undefined && c !== false && c !== ''));
  return node;
}

function setText(id, text) {
  const node = typeof id === 'string' ? $(id) : id;
  if (node.textContent !== text) node.textContent = text;
}

const isNumber = (v) => typeof v === 'number' && Number.isFinite(v);

// 80,200 · 17,481.5 · 46.9 · 1.235 · 0.1235: integers whole, else 1, 3 or 4 decimals.
function fmtNumber(v) {
  if (!isNumber(v)) return '—';
  if (v && !(Math.abs(v) >= 1e-4 && Math.abs(v) < 1e15)) return Number(v.toPrecision(3)).toExponential();
  const decimals = Number.isInteger(v) ? 0 : Math.abs(v) >= 100 ? 1 : Math.abs(v) >= 1 ? 3 : 4;
  return v.toLocaleString('en-US', { maximumFractionDigits: decimals });
}

// How `value` compares with `reference`: +17%, -0.4%, 373× (ten times or more),
// 0.05× (a tenth or less); '' when unchanged or not comparable. The sign is the
// difference's, against the reference's size, so a negative reference keeps it right
// (-1 to -2 is -100%). The × forms only for positive values: 20× of a negative
// number would not say which way it moved.
function fmtChange(value, reference) {
  if (!isNumber(value) || !isNumber(reference) || reference === 0 || value === reference) return '';
  const ratio = value / reference;
  if (value > 0 && reference > 0 && ratio >= 10) return `${ratio.toFixed(0)}×`;
  if (value > 0 && reference > 0 && ratio <= 0.1) return `${Number(ratio.toPrecision(2))}×`;
  const pct = ((value - reference) / Math.abs(reference)) * 100;
  return `${pct > 0 ? '+' : ''}${pct.toFixed(Math.abs(pct) < 1 ? 1 : 0)}%`;
}

const better = (value, reference, direction) => (direction === 'lower' ? value < reference : value > reference);

// A measured time: 48s, 1m 02s, 1h 01m.
function fmtDuration(seconds) {
  if (!isNumber(seconds)) return '';
  const s = Math.max(0, Math.floor(seconds)); // clock skew: never "-5s"
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
}

// A --time cap, and the time against it, as the flag spells it: 10m, 2h, 1h30m, 12m5s.
function fmtLimit(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return `${h ? `${h}h` : ''}${m ? `${m}m` : ''}${r || (!h && !m) ? `${r}s` : ''}`;
}

// Spent: $4.34, $4.34+ when some calls' costs are unknown; '' when none is known.
function fmtMoney(cost, incomplete = false) {
  return isNumber(cost) ? `$${cost.toFixed(2)}${incomplete ? '+' : ''}` : '';
}

// A --cost cap as given: cents ($5.00), else up to four significant digits ($0.015).
function fmtCap(v) {
  if (Math.abs(v * 100 - Math.round(v * 100)) < 1e-9) return `$${v.toFixed(2)}`;
  const decimals = Math.min(12, Math.max(2, 3 - Math.floor(Math.log10(v))));
  return `$${v.toFixed(decimals).replace(/0+$/, '').replace(/\.$/, '')}`;
}

function fmtClock(epochSeconds) {
  const t = new Date(epochSeconds * 1000);
  const time = t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return t.toDateString() === new Date().toDateString()
    ? time : `${t.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${time}`;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

// What is left of the running segment's --time, as the console says it (run_table.time_cap):
// "47m 55s left (--time 1h)", past it "time up (--time 1h)". '' when the run isn't
// running or the segment has no --time (a stop reason says when time ended it).
function timeCap(status, now = Date.now() / 1000) {
  const s = status || {}, cap = s.max_time_seconds;
  if (s.state !== 'running' || !isNumber(cap) || cap <= 0 || !isNumber(s.segment_started_at)) return '';
  const left = Math.floor(cap) - Math.max(0, Math.floor(now - s.segment_started_at));
  return `${left > 0 ? `${fmtDuration(left)} left` : 'time up'} (--time ${fmtLimit(cap)})`;
}

// "20261114-2333-sorting-1q3w" -> name "sorting-1q3w", started "2026-11-14 23:33";
// a run of autohelix.yaml has only the suffix: "Run 1q3w".
function splitRunId(id) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})-(.+)$/.exec(id || '');
  if (!m) return { name: id || null, started: '' };
  return { name: m[6].includes('-') ? m[6] : `Run ${m[6]}`, started: `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}` };
}

// The suffix every command accepts: "20261114-2333-sorting-1q3w" -> "1q3w".
const shortId = (id) => (id || '').split('-').at(-1);

// Where the run's accepted code is, in the console dashboard's words (run_table._landing):
// `landed`, `3 to land`, `3 diverged`, `—` when nothing is to say; with `manual · ` first
// for a run that lands by hand (the pages' one addition: no other column shows the mode).
// `hint` says where, or what to type. `run`: a run list row, or the
// run page's state.run (id, target_branch, landing, accepted, land).
function landingWords(run, running = false) {
  const { id, target_branch: target, landing, accepted } = run;
  const none = { text: '—', hint: 'Nothing accepted yet' };
  if (!target) return accepted ? { text: 'detached', hint: `Started on a detached HEAD: autohelix land ${shortId(id)} --into <branch>` } : none;
  if (!landing) return { text: '—', hint: `Branch ${target} or the run's commits are not available` };
  if (landing.commits === 0) return accepted ? { text: 'landed', hint: `on ${target}` } : none;
  const manual = run.land === 'manual' ? 'manual · ' : '';
  const n = plural(landing.commits, 'commit');
  if (!landing.fast_forward) {
    return { text: `${manual}${landing.commits} diverged`, hint: `${target} has moved on: git merge autohelix/${id}` };
  }
  return { text: `${manual}${landing.commits} to land`, todo: true,
    hint: running && run.land !== 'manual' ? `${n}, on ${target} when the run ends` : `${n}: autohelix land ${shortId(id)}` };
}

// A <select> of metric names (the chart's, the run list's column), refilled only when they change.
function fillMenu(menu, names, value) {
  if (Array.from(menu.options, (o) => o.value).join('\n') !== names.join('\n')) {
    menu.replaceChildren(...names.map((n) => el('option', { value: n, textContent: n })));
  }
  menu.value = value;
}

// run.json's notes (run --why, --import, archive --why), as the console's dashboard
// writes them; always shown as text.
function runNotes(run) {
  return [run.why && `why: ${run.why}`, run.import?.run && `continues from ${run.import.run}`,
    run.archive_why && `archived: ${run.archive_why}`].filter(Boolean).join(' · ');
}

// The logo for the system's light or dark theme (the colours follow it by CSS).
function followTheme(logos = { light: '/logo-light.svg', dark: '/logo-dark.svg' }) {
  const dark = matchMedia('(prefers-color-scheme: dark)');
  const apply = () => { $('logo').src = dark.matches ? logos.dark : logos.light; };
  dark.addEventListener('change', apply);
  apply();
}

if (typeof module !== 'undefined') {
  module.exports = { fmtNumber, fmtChange, better, fmtDuration, fmtLimit, fmtMoney, fmtCap, plural, timeCap,
    shortId, landingWords, splitRunId, runNotes };
}
