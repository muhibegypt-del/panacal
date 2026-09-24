"""Self-contained HTML report for an AutoCal or verification session."""
from __future__ import annotations

import html
import math
import time
from pathlib import Path

from colour import signal_code, tint

STYLE = """
:root { --bg:#fff; --fg:#1b1f24; --muted:#5b6470; --line:#d8dde3; --panel:#f5f7f9;
        --start:#c46a2b; --final:#2f6fb3; --ok:#2e7d4f; --bad:#b3261e; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#15181c; --fg:#e7eaee; --muted:#9aa4af; --line:#2c323a; --panel:#1d2126;
          --start:#e0935a; --final:#6fa8e8; --ok:#6cc08f; --bad:#ef7a72; } }
body { background:var(--bg); color:var(--fg); font:15px/1.45 system-ui, sans-serif;
       margin:0 auto; max-width:980px; padding:24px 16px 48px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:17px; margin:32px 0 8px; }
.muted { color:var(--muted); } .ok { color:var(--ok); } .bad { color:var(--bad); }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; margin-top:16px; }
.tile { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
.tile b { display:block; font-size:20px; } .tile span { color:var(--muted); font-size:13px; }
table { border-collapse:collapse; width:100%; font-size:13px; font-variant-numeric:tabular-nums; }
th, td { border-bottom:1px solid var(--line); padding:5px 6px; text-align:right; white-space:nowrap; }
th:first-child, td:first-child { text-align:left; } th { color:var(--muted); font-weight:600; }
.scroll { overflow-x:auto; } svg { width:100%; height:auto; display:block; }
svg text { fill:var(--muted); font-size:11px; } .legend { font-size:13px; color:var(--muted); }
.swatch { display:inline-block; width:10px; height:10px; border-radius:2px; margin:0 4px 0 12px; }
"""


def _e(value) -> str:
    return html.escape(str(value))


def _fmt(value, digits=4) -> str:
    return "–" if value is None else f"{value:.{digits}f}"


def _controls(values) -> str:
    """{'WB:GNR': 1, 'WB:GNB': -3} -> 'R +1  B −3'."""
    if not isinstance(values, dict):
        return "–"
    return "\u2002".join(f"{code[-1]}\u00a0{int(value):+d}".replace("-", "\u2212")
                          for code, value in values.items())


def _stages(result: dict) -> list[dict]:
    stages = result.get("stages", {})
    ordered = []
    for key in ("two_point_high", "two_point_low"):
        if key in stages:
            ordered.append(stages[key])
    ordered.extend(stages.get("detailed_white_balance", []))
    if "final_high_polish" in stages:
        ordered.append(stages["final_high_polish"])
    return ordered


def _bar_chart(levels, start, final, target) -> str:
    width, height, left, bottom, top = 720, 260, 48, 32, 12
    peak = max([v for v in list(start.values()) + list(final.values()) if v is not None] + [target * 1.5])
    scale = (height - bottom - top) / peak
    slot = (width - left - 10) / len(levels)
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Tint by level">']
    for tick in range(0, 5):
        value = peak * tick / 4
        y = height - bottom - value * scale
        parts.append(f'<line x1="{left}" x2="{width - 10}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--line)"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{value:.4f}</text>')
    y = height - bottom - target * scale
    parts.append(f'<line x1="{left}" x2="{width - 10}" y1="{y:.1f}" y2="{y:.1f}" '
                 f'stroke="var(--ok)" stroke-dasharray="4 4"/>')
    for index, level in enumerate(levels):
        x = left + index * slot
        bar = slot * 0.34
        for offset, series, colour in ((0.14, start, "var(--start)"), (0.52, final, "var(--final)")):
            value = series.get(level)
            if value is None:
                continue
            h = value * scale
            parts.append(f'<rect x="{x + slot * offset:.1f}" y="{height - bottom - h:.1f}" '
                         f'width="{bar:.1f}" height="{h:.1f}" rx="2" fill="{colour}">'
                         f'<title>{level}%: {value:.5f}</title></rect>')
        parts.append(f'<text x="{x + slot / 2:.1f}" y="{height - bottom + 16}" '
                     f'text-anchor="middle">{level}%</text>')
    parts.append("</svg>")
    return "".join(parts)


def _prediction_chart(points) -> str:
    size, pad = 320, 40
    peak = max([max(p, m) for p, m, _ in points] + [0.001]) * 1.1
    scale = (size - 2 * pad) / peak
    parts = [f'<svg viewBox="0 0 {size} {size}" style="max-width:360px" role="img" '
             f'aria-label="Predicted versus measured tint">']
    parts.append(f'<line x1="{pad}" y1="{size - pad}" x2="{size - pad}" y2="{pad}" '
                 f'stroke="var(--line)" stroke-dasharray="4 4"/>')
    parts.append(f'<rect x="{pad}" y="{pad}" width="{size - 2 * pad}" height="{size - 2 * pad}" '
                 f'fill="none" stroke="var(--line)"/>')
    for predicted, measured, accepted in points:
        colour = "var(--ok)" if accepted else "var(--bad)"
        parts.append(f'<circle cx="{pad + predicted * scale:.1f}" cy="{size - pad - measured * scale:.1f}" '
                     f'r="4" fill="{colour}" fill-opacity="0.75"><title>predicted {predicted:.5f}, '
                     f'measured {measured:.5f}</title></circle>')
    parts.append(f'<text x="{size / 2}" y="{size - 8}" text-anchor="middle">predicted tint (u\'v\')</text>')
    parts.append(f'<text x="12" y="{size / 2}" text-anchor="middle" '
                 f'transform="rotate(-90 12 {size / 2})">measured tint</text>')
    parts.append(f'<text x="{size - pad}" y="{size - pad + 14}" text-anchor="end">{peak:.4f}</text>')
    parts.append("</svg>")
    return "".join(parts)


def build_report(result: dict, config: dict, title: str) -> str:
    summary = result.get("final_summary") or result.get("summary") or {}
    final_rows = result.get("final") or result.get("measurements") or []
    stages = _stages(result)
    signal_range = config["pattern"]["range"]
    stop_uv = float(config["solver"]["white_balance_stop_uv"])
    moves = [item for stage in stages for item in stage["history"] if "move" in item]
    probes = sum(1 for stage in stages for item in stage["history"] if item.get("responses"))

    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>AutoCal report</title><style>{STYLE}</style></head><body>"]
    out.append(f"<h1>{_e(title)}</h1><div class='muted'>{_e(time.strftime('%Y-%m-%d %H:%M'))}"
               f" · target D65, gamma {config['target']['gamma']}</div>")

    if summary:
        tiles = [
            (_fmt(summary.get("average_tint_uv"), 5), "average tint (u'v' from D65)"),
            (f"{_fmt(summary.get('maximum_tint_uv'), 5)} @ {summary.get('worst_tint_level')}%",
             "worst tint"),
            (_fmt(summary.get("average_chroma_delta_e_2000"), 2), "average chroma dE2000"),
            (_fmt(summary.get("average_delta_e_2000"), 2), "average dE2000 incl. luminance"),
            (_fmt(summary.get("median_effective_gamma"), 3), "measured gamma (median)"),
        ]
        if moves:
            tiles.append((f"{sum(1 for m in moves if m['accepted'])}/{len(moves)}",
                          f"moves kept · {probes} probes"))
        out.append("<div class='tiles'>" + "".join(
            f"<div class='tile'><b>{_e(v)}</b><span>{_e(k)}</span></div>" for v, k in tiles) + "</div>")

    if final_rows:
        final = {row.level: tint(row) for row in final_rows if row.level > 0}
        start = {}
        if result.get("reference_white") is not None:
            start[100] = tint(result["reference_white"])
        for stage in stages:
            first = (stage.get("start") or {}).get("measurement")
            if first is not None and first.level not in start:
                start[first.level] = tint(first)
        levels = sorted(final)
        out.append("<h2>Tint by level</h2>")
        if start:
            out.append("<div class='legend'><span class='swatch' style='background:var(--start)'></span>"
                       "before its correction<span class='swatch' style='background:var(--final)'></span>"
                       "final verification<span class='swatch' style='background:var(--ok)'></span>"
                       f"stop target {stop_uv:.4f}</div>")
        out.append(_bar_chart(levels, start, final, stop_uv))

    if moves:
        out.append("<h2>Predicted versus measured</h2><p class='muted'>Each dot is one move chosen by "
                   "simulation. On the diagonal the model predicted the result exactly; green kept, "
                   "red restored.</p>")
        out.append(_prediction_chart([(math.hypot(*m["predicted_error"]), math.hypot(*m["measured_error"]),
                                       m["accepted"]) for m in moves]))

    if stages:
        out.append("<h2>Controls per point</h2><div class='scroll'><table><tr><th>Stage</th><th>Patch</th>"
                   "<th>Start</th><th>Final</th><th>Tint before</th><th>Tint after</th><th>Kept</th>"
                   "<th>Model</th></tr>")
        for stage in stages:
            begin = stage.get("start") or {}
            end = stage.get("end") or {}
            kept = [m for m in stage["history"] if "move" in m]
            source = next((m.get("model_source") for m in reversed(stage["history"])
                           if m.get("model_source")), None)
            if source is None:
                reasons = [m.get("reason") for m in stage["history"] if m.get("reason")]
                source = reasons[-1].replace("_", " ") if reasons else "–"
            before_row, after_row = begin.get("measurement"), end.get("measurement")
            out.append(
                f"<tr><td>{_e(stage['label'])}</td><td>{stage['primary']}%</td>"
                f"<td>{_e(_controls(begin.get('controls')))}</td><td>{_e(_controls(end.get('controls')))}</td>"
                f"<td>{_fmt(tint(before_row) if before_row else None, 5)}</td>"
                f"<td>{_fmt(tint(after_row) if after_row else None, 5)}</td>"
                f"<td>{sum(1 for m in kept if m['accepted'])}/{len(kept)}</td><td>{_e(source)}</td></tr>")
        out.append("</table></div>")

    if final_rows:
        out.append("<h2>Final verification</h2><div class='scroll'><table><tr><th>Level</th><th>Code</th>"
                   "<th>Y cd/m²</th><th>x</th><th>y</th><th>Tint u'v'</th><th>Chroma dE00</th>"
                   "<th>dE00</th></tr>")
        points = {p["measurement"].level: p["metrics"] for p in summary.get("points", [])}
        for row in sorted(final_rows, key=lambda r: r.level):
            item = points.get(row.level)
            value = tint(row)
            css = "ok" if value <= stop_uv else ("bad" if value > 3 * stop_uv else "")
            out.append(
                f"<tr><td>{row.level}%</td><td>{signal_code(row.level, signal_range)}</td>"
                f"<td>{row.xyz.Y:.3f}</td><td>{row.x:.4f}</td><td>{row.y:.4f}</td>"
                f"<td class='{css}'>{value:.5f}</td>"
                f"<td>{_fmt(item.chroma_delta_e if item and row.level else None, 2)}</td>"
                f"<td>{_fmt(item.total_delta_e if item and row.level else None, 2)}</td></tr>")
        out.append("</table></div>")

    if moves:
        out.append("<h2>Every move</h2><div class='scroll'><table><tr><th>Stage</th><th>#</th><th>Model</th>"
                   "<th>Tried</th><th>Predicted</th><th>Measured</th><th>Threshold</th><th>Result</th></tr>")
        for stage in stages:
            for m in stage["history"]:
                if "move" not in m:
                    continue
                out.append(
                    f"<tr><td>{_e(stage['label'])}</td><td>{m['iteration']}</td><td>{_e(m['model_source'])}</td>"
                    f"<td>{_e(_controls(m['controls_tried']))}</td><td>{math.hypot(*m['predicted_error']):.5f}</td>"
                    f"<td>{math.hypot(*m['measured_error']):.5f}</td><td>{m['threshold']:.5f}</td>"
                    f"<td class='{'ok' if m['accepted'] else 'bad'}'>"
                    f"{'kept' if m['accepted'] else 'restored'}</td></tr>")
        out.append("</table></div>")
    out.append("</body></html>")
    return "".join(out)


def write_report(path: Path, result: dict, config: dict, title: str) -> Path:
    path.write_text(build_report(result, config, title), encoding="utf-8")
    return path
