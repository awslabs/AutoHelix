# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Generate a self-contained HTML dashboard from iteration history."""

import json
from pathlib import Path

from autohelix.history import History, IterationResult


def generate_dashboard(project_path: Path, directions: dict[str, str] | None = None) -> Path:
    """Generate .autohelix/output/dashboard.html and return its path."""
    history = History(project_path)
    results = history.load()
    directions = directions or {}

    output_path = project_path / ".autohelix" / "output" / "dashboard.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data = _build_data(results, directions)
    html = _render_html(data)
    output_path.write_text(html)
    return output_path


def _build_data(results: list[IterationResult], directions: dict[str, str]) -> dict:
    """Build structured data for the template."""
    baseline = next((r for r in results if r.iteration == 0), None)
    iterations = sorted((r for r in results if r.iteration > 0), key=lambda r: r.iteration)

    metric_names = set()
    for r in results:
        metric_names.update(r.metrics.keys())
    metric_names = sorted(metric_names)

    return {
        "baseline": baseline,
        "iterations": iterations,
        "metric_names": metric_names,
        "directions": directions,
    }


def _render_html(data: dict) -> str:
    """Render the full HTML dashboard."""
    baseline = data["baseline"]
    iterations = data["iterations"]
    metric_names = data["metric_names"]
    directions = data["directions"]

    # Prepare JSON data for the JS chart rendering (include baseline as iter 0)
    chart_data = []
    if baseline:
        chart_data.append({
            "iter": 0,
            "accepted": True,
            "metrics": baseline.metrics,
            "reason": "baseline",
            "cost": baseline.usage.get("cost_usd", 0),
            "time_s": baseline.usage.get("wall_clock_ms", baseline.usage.get("duration_ms", 0)) / 1000,
            "is_baseline": True,
        })
    for r in iterations:
        chart_data.append({
            "iter": r.iteration,
            "accepted": r.accepted,
            "metrics": r.metrics,
            "reason": r.reason,
            "cost": r.usage.get("cost_usd", 0),
            "time_s": r.usage.get("wall_clock_ms", r.usage.get("duration_ms", 0)) / 1000,
            "is_baseline": False,
        })

    baseline_metrics = baseline.metrics if baseline else {}

    # Summary stats
    total_iters = len(iterations)
    accepted = sum(1 for r in iterations if r.accepted)
    rejected = total_iters - accepted
    total_cost = sum(r.usage.get("cost_usd", 0) for r in iterations)
    total_time_s = sum(
        r.usage.get("wall_clock_ms", r.usage.get("duration_ms", 0))
        for r in iterations
    ) / 1000

    # Best metrics
    best_metrics = {}
    for name in metric_names:
        lower_is_better = directions.get(name, "higher") == "lower"
        best_val = None
        best_iter = 0
        for r in iterations:
            if not r.accepted or name not in r.metrics:
                continue
            val = r.metrics[name]
            if best_val is None:
                best_val, best_iter = val, r.iteration
            elif lower_is_better and val < best_val:
                best_val, best_iter = val, r.iteration
            elif not lower_is_better and val > best_val:
                best_val, best_iter = val, r.iteration
        if best_val is not None:
            bl = baseline_metrics.get(name)
            pct = None
            if bl and bl != 0:
                pct = (best_val - bl) / bl * 100
            best_metrics[name] = {"value": best_val, "iter": best_iter, "pct": pct}

    chart_data_json = json.dumps(chart_data)
    baseline_json = json.dumps(baseline_metrics)
    directions_json = json.dumps(directions)
    metric_names_json = json.dumps(metric_names)

    # Format time
    if total_time_s >= 3600:
        time_str = f"{total_time_s / 3600:.1f}h"
    elif total_time_s >= 60:
        time_str = f"{total_time_s / 60:.0f}m {int(total_time_s) % 60}s"
    else:
        time_str = f"{total_time_s:.0f}s"

    # Best metrics HTML
    best_html_parts = []
    for name, info in best_metrics.items():
        direction = directions.get(name, "higher")
        arrow = "↑" if direction == "higher" else "↓"
        pct_str = ""
        if info["pct"] is not None:
            pct_str = f' <span class="delta">({arrow} {abs(info["pct"]):.1f}%)</span>'
        best_html_parts.append(
            f'<div class="stat"><div class="stat-value">{info["value"]:g}{pct_str}</div>'
            f'<div class="stat-label">{name} (iter {info["iter"]})</div></div>'
        )
    best_html = "\n".join(best_html_parts) if best_html_parts else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>AutoHelix Dashboard</title>
<style>
:root {{
  --bg: #0d1117;
  --surface: #161b22;
  --border: #30363d;
  --text: #e6edf3;
  --text-dim: #8b949e;
  --accent: #58a6ff;
  --green: #3fb950;
  --red: #f85149;
  --yellow: #d29922;
  --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  --mono: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  font-size: 14px;
  line-height: 1.5;
  padding: 24px;
  max-width: 1200px;
  margin: 0 auto;
}}
h1 {{
  font-size: 20px;
  font-weight: 600;
  margin-bottom: 4px;
}}
.subtitle {{
  color: var(--text-dim);
  font-size: 13px;
  margin-bottom: 24px;
}}
.stats-row {{
  display: flex;
  gap: 16px;
  margin-bottom: 24px;
  flex-wrap: wrap;
}}
.stat {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
  min-width: 140px;
}}
.stat-value {{
  font-size: 20px;
  font-weight: 600;
  font-family: var(--mono);
}}
.stat-label {{
  color: var(--text-dim);
  font-size: 12px;
  margin-top: 2px;
}}
.delta {{
  font-size: 14px;
  color: var(--green);
}}
.chart-container {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 16px;
}}
.chart-title {{
  font-size: 13px;
  font-weight: 600;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 12px;
}}
.chart-svg {{
  width: 100%;
  height: 180px;
}}
.iteration-table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  font-family: var(--mono);
}}
.iteration-table th {{
  text-align: left;
  color: var(--text-dim);
  font-weight: 500;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}}
.iteration-table td {{
  padding: 6px 12px;
  border-bottom: 1px solid var(--border);
}}
.iteration-table tr:hover td {{
  background: rgba(88, 166, 255, 0.04);
}}
.badge {{
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 500;
}}
.badge-merged {{ background: rgba(63, 185, 80, 0.15); color: var(--green); }}
.badge-rejected {{ background: rgba(248, 81, 73, 0.15); color: var(--red); }}
#chart-tooltip {{
  position: fixed;
  background: var(--surface);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 12px;
  font-family: var(--mono);
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.15s;
  z-index: 100;
  line-height: 1.6;
  box-shadow: 0 4px 12px rgba(0,0,0,0.4);
}}
#chart-tooltip.visible {{ opacity: 1; }}
.hit-target {{ cursor: pointer; }}
.hit-target:hover + circle {{ r: 6; }}
</style>
</head>
<body>
<h1>AutoHelix</h1>
<div class="subtitle">Auto-refresh every 10s &middot; {total_iters} iterations &middot; {time_str}</div>

<div class="stats-row">
  <div class="stat">
    <div class="stat-value">{accepted}<span style="color:var(--text-dim);font-size:14px">/{total_iters}</span></div>
    <div class="stat-label">merged</div>
  </div>
  <div class="stat">
    <div class="stat-value">${total_cost:.2f}</div>
    <div class="stat-label">total cost</div>
  </div>
  <div class="stat">
    <div class="stat-value">{time_str}</div>
    <div class="stat-label">total time</div>
  </div>
  {best_html}
</div>

<div id="chart-tooltip"></div>
<div id="charts"></div>

<div class="chart-container">
  <div class="chart-title">Iterations</div>
  <table class="iteration-table">
    <thead>
      <tr>
        <th>Iter</th>
        <th>Status</th>
        {"".join(f'<th>{n}</th>' for n in metric_names)}
        <th>Cost</th>
        <th>Time</th>
        <th>Reason</th>
      </tr>
    </thead>
    <tbody id="iter-tbody"></tbody>
  </table>
</div>

<script>
const data = {chart_data_json};
const baseline = {baseline_json};
const directions = {directions_json};
const metricNames = {metric_names_json};

// Tooltip helper
const tip = document.getElementById('chart-tooltip');
function showTip(evt, html) {{
  tip.innerHTML = html;
  tip.classList.add('visible');
  tip.style.left = (evt.clientX + 12) + 'px';
  tip.style.top = (evt.clientY - 10) + 'px';
}}
function hideTip() {{ tip.classList.remove('visible'); }}

// HTML escape for untrusted strings
function esc(s) {{
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}}

// Render iteration table
const tbody = document.getElementById('iter-tbody');
data.forEach(d => {{
  let statusBadge;
  if (d.is_baseline) {{
    statusBadge = '<span class="badge" style="background:rgba(210,153,34,0.15);color:var(--yellow)">baseline</span>';
  }} else if (d.accepted) {{
    statusBadge = '<span class="badge badge-merged">merged</span>';
  }} else {{
    statusBadge = '<span class="badge badge-rejected">rejected</span>';
  }}
  const metricCells = metricNames.map(name => {{
    if (d.metrics[name] !== undefined) {{
      const val = d.metrics[name];
      const bl = baseline[name];
      let delta = '';
      if (!d.is_baseline && bl && bl !== 0) {{
        const pct = ((val - bl) / bl * 100);
        const dir = directions[name] || 'higher';
        const arrow = pct < 0 ? '↓' : '↑';
        const good = (dir === 'lower' && pct < 0) || (dir === 'higher' && pct > 0);
        const color = good ? 'var(--green)' : 'var(--red)';
        delta = ` <span style="color:${{color}};font-size:11px">(${{arrow}} ${{Math.abs(pct).toFixed(1)}}%)</span>`;
      }}
      return `<td>${{Number(val.toPrecision(4))}}${{delta}}</td>`;
    }}
    return '<td style="color:var(--text-dim)">—</td>';
  }}).join('');
  const timeStr = d.time_s >= 60
    ? `${{Math.floor(d.time_s/60)}}m ${{Math.floor(d.time_s%60)}}s`
    : `${{Math.floor(d.time_s)}}s`;
  const reason = d.is_baseline ? '' : (d.reason ? `<span style="color:var(--text-dim)">${{esc(d.reason)}}</span>` : '');
  tbody.innerHTML += `<tr>
    <td>${{d.iter}}</td>
    <td>${{statusBadge}}</td>
    ${{metricCells}}
    <td>${{d.is_baseline ? '—' : '$' + d.cost.toFixed(3)}}</td>
    <td>${{d.is_baseline ? '—' : timeStr}}</td>
    <td>${{reason}}</td>
  </tr>`;
}});

// Render metric charts
const chartsDiv = document.getElementById('charts');
metricNames.forEach(name => {{
  const points = data.filter(d => d.metrics[name] !== undefined);
  if (points.length === 0) return;

  const container = document.createElement('div');
  container.className = 'chart-container';
  container.innerHTML = `<div class="chart-title">${{name}} (${{directions[name] || 'higher'}} is better)</div>`;

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'chart-svg');
  svg.setAttribute('viewBox', '0 0 800 180');
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');

  const values = points.map(p => p.metrics[name]);
  const minVal = Math.min(...values);
  const maxVal = Math.max(...values);
  const range = maxVal - minVal || 1;
  const padding = range * 0.1;
  const yMin = minVal >= 0 ? Math.max(0, minVal - padding) : minVal - padding;
  const yMax = maxVal + padding;

  const xScale = (i) => 100 + (i / Math.max(points.length - 1, 1)) * 660;
  const yScale = (v) => 155 - ((v - yMin) / (yMax - yMin)) * 130;

  // Grid lines
  for (let i = 0; i <= 4; i++) {{
    const y = 25 + i * 32.5;
    const val = yMax - (i / 4) * (yMax - yMin);
    svg.innerHTML += `<line x1="80" y1="${{y}}" x2="760" y2="${{y}}" stroke="#30363d" stroke-width="0.5"/>`;
    svg.innerHTML += `<text x="74" y="${{y+4}}" text-anchor="end" fill="#8b949e" font-size="10" font-family="monospace">${{Number(val.toPrecision(3))}}</text>`;
  }}

  // Baseline reference line
  const blVal = baseline[name];
  if (blVal !== undefined) {{
    const blY = yScale(blVal);
    svg.innerHTML += `<line x1="80" y1="${{blY}}" x2="760" y2="${{blY}}" stroke="#d29922" stroke-width="1" stroke-dasharray="6,4" opacity="0.6"/>`;
    svg.innerHTML += `<text x="764" y="${{blY - 4}}" fill="#d29922" font-size="9" font-family="monospace" opacity="0.8">baseline</text>`;
  }}

  // Line path
  if (points.length > 1) {{
    const pathD = points.map((p, i) => {{
      const x = xScale(i);
      const y = yScale(p.metrics[name]);
      return (i === 0 ? 'M' : 'L') + ` ${{x}} ${{y}}`;
    }}).join(' ');
    svg.innerHTML += `<path d="${{pathD}}" fill="none" stroke="#58a6ff" stroke-width="2"/>`;
  }}

  // Points with hit targets and tooltips
  points.forEach((p, i) => {{
    const x = xScale(i);
    const y = yScale(p.metrics[name]);
    let color;
    if (p.is_baseline) {{
      color = '#d29922';
    }} else if (p.accepted) {{
      color = '#3fb950';
    }} else {{
      color = '#f85149';
    }}
    // Invisible larger hit target
    svg.innerHTML += `<circle cx="${{x}}" cy="${{y}}" r="8" fill="transparent" class="hit-target" data-metric="${{name}}" data-idx="${{i}}"/>`;
    svg.innerHTML += `<circle cx="${{x}}" cy="${{y}}" r="4" fill="${{color}}"/>`;
  }});

  container.appendChild(svg);

  // Attach tooltip events after SVG is in the DOM
  svg.querySelectorAll('.hit-target').forEach(el => {{
    const idx = +el.dataset.idx;
    const p = points[idx];
    el.addEventListener('mousemove', (evt) => {{
      const val = p.metrics[name];
      const bl = baseline[name];
      let deltaStr = '';
      if (!p.is_baseline && bl && bl !== 0) {{
        const pct = ((val - bl) / bl * 100);
        const dir = directions[name] || 'higher';
        const good = (dir === 'lower' && pct < 0) || (dir === 'higher' && pct > 0);
        const color = good ? 'var(--green)' : 'var(--red)';
        const arrow = pct > 0 ? '+' : '';
        deltaStr = `<br><span style="color:${{color}}">${{arrow}}${{pct.toFixed(1)}}% from baseline</span>`;
      }}
      const label = p.is_baseline ? 'Baseline' : `Iter ${{p.iter}} (${{p.accepted ? 'merged' : 'rejected'}})`;
      showTip(evt, `<b>${{label}}</b><br>${{name}}: ${{Number(val.toPrecision(4))}}${{deltaStr}}`);
    }});
    el.addEventListener('mouseleave', hideTip);
  }});

  chartsDiv.appendChild(container);
}});

// Cost chart (skip baseline)
const iterData = data.filter(d => !d.is_baseline);
if (iterData.length > 0) {{
  const container = document.createElement('div');
  container.className = 'chart-container';
  container.innerHTML = '<div class="chart-title">Cost per iteration</div>';

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'chart-svg');
  svg.setAttribute('viewBox', '0 0 800 180');
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');

  const costs = iterData.map(d => d.cost);
  const maxCost = Math.max(...costs, 0.01);

  const slotWidth = 700 / iterData.length;
  const barWidth = Math.min(40, slotWidth - 4);
  iterData.forEach((d, i) => {{
    const x = 60 + i * slotWidth + (slotWidth - barWidth) / 2;
    const h = (d.cost / maxCost) * 130;
    const y = 155 - h;
    const color = d.accepted ? '#3fb950' : '#f85149';
    svg.innerHTML += `<rect x="${{x}}" y="${{y}}" width="${{barWidth}}" height="${{h}}" fill="${{color}}" opacity="0.7" rx="2" class="cost-bar" data-idx="${{i}}"/>`;
    svg.innerHTML += `<text x="${{60 + i * slotWidth + slotWidth/2}}" y="172" text-anchor="middle" fill="#8b949e" font-size="9" font-family="monospace">${{d.iter}}</text>`;
  }});

  // Y-axis labels
  for (let i = 0; i <= 4; i++) {{
    const y = 25 + i * 32.5;
    const val = maxCost - (i / 4) * maxCost;
    svg.innerHTML += `<line x1="60" y1="${{y}}" x2="760" y2="${{y}}" stroke="#30363d" stroke-width="0.5"/>`;
    svg.innerHTML += `<text x="55" y="${{y+4}}" text-anchor="end" fill="#8b949e" font-size="10" font-family="monospace">$${{val.toFixed(2)}}</text>`;
  }}

  container.appendChild(svg);

  // Cost bar tooltips
  svg.querySelectorAll('.cost-bar').forEach(el => {{
    const d = iterData[+el.dataset.idx];
    el.style.cursor = 'pointer';
    el.addEventListener('mousemove', (evt) => {{
      const timeStr = d.time_s >= 60
        ? `${{Math.floor(d.time_s/60)}}m ${{Math.floor(d.time_s%60)}}s`
        : `${{Math.floor(d.time_s)}}s`;
      showTip(evt, `<b>Iter ${{d.iter}}</b> (${{d.accepted ? 'merged' : 'rejected'}})<br>Cost: $${{d.cost.toFixed(3)}}<br>Time: ${{timeStr}}`);
    }});
    el.addEventListener('mouseleave', hideTip);
  }});

  chartsDiv.appendChild(container);
}}
</script>
</body>
</html>"""
