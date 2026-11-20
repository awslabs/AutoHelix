// AutoHelix web pages: what the run list (dashboard.js) and the run page (viewer.js)
// share: elements, number and time formats, the theme button.
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

// A metric value: four significant digits, one decimal from 100 up.
function fmtNumber(v) {
  if (!isNumber(v)) return '—';
  return Math.abs(v) >= 100 ? v.toFixed(1) : Number(v.toPrecision(4)).toString();
}

// How `value` compares with `baseline`: ↑ 17.0%, ↓ 0.4%, ↑ 12x; '' when unchanged or not
// comparable. The arrow is the difference's direction, against the baseline's size, so a
// negative baseline keeps it right. The x forms only for positive values (12x of a
// negative number would not say which way it moved).
function formatDelta(value, baseline) {
  if (!isNumber(value) || !isNumber(baseline) || baseline === 0 || value === baseline) return '';
  const pct = ((value - baseline) / Math.abs(baseline)) * 100;
  const arrow = pct < 0 ? '↓' : '↑';
  const abs = Math.abs(pct);
  const positive = value > 0 && baseline > 0;
  if (positive && pct > 0 && abs >= 900) return `${arrow} ${(abs / 100 + 1).toFixed(0)}x`;
  if (positive && pct < 0 && abs >= 90 && 100 / (100 - abs) >= 10) return `${arrow} ${(100 / (100 - abs)).toFixed(0)}x`;
  return `${arrow} ${abs.toFixed(1)}%`;
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

// "20261114-2333-sorting-1q3w" -> name "sorting-1q3w", started "2026-11-14 23:33";
// a run of autohelix.yaml has only the suffix: "Run 1q3w".
function splitRunId(id) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})-(.+)$/.exec(id || '');
  if (!m) return { name: id || null, started: '' };
  return { name: m[6].includes('-') ? m[6] : `Run ${m[6]}`, started: `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}` };
}

// A <select> of metric names (the chart's, the run list's column), refilled only when they change.
function fillMenu(menu, names, value) {
  if (Array.from(menu.options, (o) => o.value).join('\n') !== names.join('\n')) {
    menu.replaceChildren(...names.map((n) => el('option', { value: n, textContent: n })));
  }
  menu.value = value;
}

// "Why: … · continues from k3f9 · archived: …" from run.json's notes (shown as text).
function runNotes(run) {
  return [run.why && `Why: ${run.why}`, run.import?.run && `continues from ${run.import.run}`,
    run.archive_why && `archived: ${run.archive_why}`].filter(Boolean).join(' · ');
}

// Where the run's files are (report.md, history.jsonl, iterations/), from the project's
// folder, as the end block of `run` names it.
function runFolder(runId) {
  return runId ? `.autohelix/runs/${runId}/` : '';
}

// The theme button: Auto (follow the system) -> Light -> Dark, remembered in this
// browser (both pages share the key); the logo follows the theme in use.
const THEMES = { auto: '◐ Auto', light: '☀ Light', dark: '☾ Dark' };

function themeButton(logos = { light: '/logo-light.svg', dark: '/logo-dark.svg' }) {
  const system = matchMedia('(prefers-color-scheme: dark)');
  let theme = 'auto';
  try { theme = localStorage.getItem('autohelix-theme') || 'auto'; } catch { /* storage blocked */ }
  if (!(theme in THEMES)) theme = 'auto';
  const apply = () => {
    if (theme === 'auto') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    const dark = theme === 'dark' || (theme === 'auto' && system.matches);
    $('logo').src = dark ? logos.dark : logos.light;
    $('theme').textContent = THEMES[theme];
  };
  $('theme').addEventListener('click', () => {
    const order = Object.keys(THEMES);
    theme = order[(order.indexOf(theme) + 1) % order.length];
    try { localStorage.setItem('autohelix-theme', theme); } catch { /* still applies to this page */ }
    apply();
  });
  system.addEventListener('change', apply);
  apply();
}

if (typeof module !== 'undefined') {
  module.exports = { fmtNumber, formatDelta, better, fmtDuration, fmtLimit, fmtMoney, fmtCap, plural,
    splitRunId, runNotes, runFolder, themeButton };
}
