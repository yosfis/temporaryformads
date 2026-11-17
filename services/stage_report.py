"""Stage reports — every stage ends with a decision + plain-English summary.

A report is stored in stage_state.notes["report"] so it survives page reloads:
    {stage, stage_label, decision, decision_text, summary,
     highlights: [{label, value}], sections: [{title, items: [str]}],
     links: [{label, url}], created_at}
"""
from __future__ import annotations
import json
import re

from config import STAGE_LABELS
from memory.database import connect
from memory.schemas import utcnow

DECISION_TEXT = {
    "GO": "Go ahead — ready for the next stage",
    "REPEAT": "Repeat — more work needed before moving on",
    "PIVOT": "Pivot — adjust the direction before continuing",
    "STOP": "Stop — not feasible as currently described",
}

_SNAKE = re.compile(r"\b[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+\b")


def humanize(text):
    """'feasibility_gate' -> 'feasibility gate'; leaves URLs and normal prose alone."""
    if text is None:
        return ""
    text = str(text)
    if "://" in text:
        return text
    return _SNAKE.sub(lambda m: m.group(0).replace("_", " "), text)


def label(key):
    """Turn a key into a title: 'gross_margin_pct' -> 'Gross margin %'."""
    s = humanize(key).replace(" pct", " %").strip()
    return s[:1].upper() + s[1:]


def _clean(obj):
    if isinstance(obj, str):
        return humanize(obj)
    if isinstance(obj, list):
        return [_clean(x) for x in obj]
    if isinstance(obj, dict):
        return {k: (v if k in ("url", "stage", "decision") else _clean(v)) for k, v in obj.items()}
    return obj


def build(stage, decision, summary, *, highlights=None, sections=None, links=None):
    decision = decision if decision in DECISION_TEXT else "REPEAT"
    report = {
        "stage": stage,
        "stage_label": STAGE_LABELS.get(stage, stage.title()),
        "decision": decision,
        "decision_text": DECISION_TEXT[decision],
        "summary": summary,
        "highlights": [{"label": str(k), "value": str(v)} for k, v in (highlights or []) if v not in (None, "")],
        "sections": [{"title": t, "items": [str(i) for i in items if str(i).strip()]}
                     for t, items in (sections or []) if items],
        "links": [{"label": l, "url": u} for l, u in (links or [])],
        "created_at": utcnow(),
    }
    return _clean(report)


def save(vid, stage, report):
    with connect() as c:
        row = c.execute("SELECT notes_json FROM stage_state WHERE venture_id=? AND stage=?",
                        (vid, stage)).fetchone()
        notes = {}
        if row and row["notes_json"]:
            try:
                notes = json.loads(row["notes_json"]) or {}
            except Exception:
                notes = {}
        notes["report"] = report
        c.execute("UPDATE stage_state SET notes_json=?, updated_at=? WHERE venture_id=? AND stage=?",
                  (json.dumps(notes), utcnow(), vid, stage))


def get(vid, stage):
    with connect() as c:
        row = c.execute("SELECT notes_json FROM stage_state WHERE venture_id=? AND stage=?",
                        (vid, stage)).fetchone()
    try:
        return (json.loads(row["notes_json"]) or {}).get("report") if row else None
    except Exception:
        return None


def summarize(client, stage, decision, facts, fallback):
    """2–3 sentence plain-English summary of what the stage found. Never raises."""
    try:
        out = client.chat_json(
            messages=[{"role": "user", "content": json.dumps({
                "stage": STAGE_LABELS.get(stage, stage), "decision": DECISION_TEXT.get(decision, decision),
                "results": facts,
                "instruction": ("Write a 2-3 sentence summary for the founder of what this stage did, what it "
                                "found, and why the decision was made. Plain, friendly English. "
                                "No snake_case, no JSON keys, no jargon, no markdown."),
            }, ensure_ascii=False, default=str)}],
            system="You explain startup analysis results clearly to first-time founders.",
            schema_hint='{"summary": "string"}')
        s = str(out.get("summary") or "").strip()
        return humanize(s) if s else fallback
    except Exception:
        return fallback
