"""Compile every stage report + the business map into one shareable HTML file."""
from __future__ import annotations
import html
from pathlib import Path

from config import STAGES
from services import stage_report


def write(venture, out_file: Path):
    e = html.escape
    brief = venture.get("brief") or {}
    bmap = brief.get("business_map") or {}
    parts = [f"<h1>{e(venture.get('name') or 'Venture')}</h1>",
             f"<p class='lead'>{e(venture.get('idea') or '')}</p>"]
    if bmap.get("aspects"):
        parts.append("<h2>Business map — 12 core aspects</h2><div class='grid'>")
        for a in bmap["aspects"]:
            pts = "".join(f"<li>{e(p)}</li>" for p in a.get("points") or [])
            parts.append(f"<div class='aspect {e(a.get('status', ''))}'><h3>{e(a['label'])}</h3>"
                         f"<p>{e(a.get('summary', ''))}</p><ul>{pts}</ul></div>")
        parts.append("</div>")
    for stage in STAGES:
        r = stage_report.get(venture["id"], stage)
        if not r:
            continue
        parts.append(f"<h2>{e(r['stage_label'])} <span class='badge'>{e(r['decision_text'])}</span></h2>"
                     f"<p>{e(r['summary'])}</p>")
        if r.get("highlights"):
            parts.append("<table>" + "".join(f"<tr><th>{e(h['label'])}</th><td>{e(h['value'])}</td></tr>"
                                             for h in r["highlights"]) + "</table>")
        for s in r.get("sections") or []:
            parts.append(f"<h3>{e(s['title'])}</h3><ul>" + "".join(f"<li>{e(i)}</li>" for i in s["items"]) + "</ul>")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(venture.get('name') or 'Venture')} — Venture report</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:960px;margin:auto;padding:24px 16px;line-height:1.55;color:#1a1a22}}
h1{{margin-bottom:4px}} .lead{{color:#555;font-size:18px}} h2{{margin-top:36px;border-bottom:2px solid #eee;padding-bottom:6px}}
.badge{{font-size:13px;font-weight:600;background:#eef;color:#336;padding:3px 10px;border-radius:999px;margin-left:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}}
.aspect{{border:1px solid #ddd;border-radius:10px;padding:12px}} .aspect h3{{margin:0 0 6px}}
.aspect.strong{{border-color:#22c55e}} .aspect.gap{{border:1px dashed #ef4444}}
table{{border-collapse:collapse}} th,td{{text-align:left;padding:4px 12px 4px 0}} th{{color:#666;font-weight:500}}
@media print{{h2{{break-after:avoid}}}}
</style></head><body>{''.join(parts)}</body></html>""", encoding="utf-8")
    return out_file
