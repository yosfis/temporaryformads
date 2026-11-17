"""Mind-map data — the 12-aspect business map, generated after the Discover stage."""
from __future__ import annotations
from memory import database

LEAF_MAX = 55
ASPECT_MAX = 26

# Discover has produced research once it reaches any of these statuses.
DISCOVER_DONE = ("awaiting_approval", "completed", "stopped")


def _cut(s, n):
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def discover_done(vid):
    stages = database.get_stages(vid)
    return (stages.get("discover") or {}).get("status") in DISCOVER_DONE


def _actions(v, bmap):
    gate = v.get("gate", "pending")
    current = v.get("current_stage", "discover")
    if gate == "STOP":
        actions = ["Pause — feasibility check failed", "Draft a pivot or different approach"]
    elif gate == "PIVOT":
        actions = ["Identify the adjacent opportunity", "Re-draft the brief with the new direction"]
    elif gate == "REPEAT":
        actions = ["Gather additional evidence", "Confirm assumptions with customers"]
    elif gate == "GO":
        actions = [f"Approve '{current.title()}' and advance"]
    else:
        actions = []
    actions += bmap.get("next_actions") or []
    gaps = [a["label"] for a in bmap["aspects"] if a.get("status") == "gap"]
    if gaps:
        actions.append("Fill gaps: " + ", ".join(gaps))
    return actions


def build(vid):
    v = database.get_venture(vid)
    if not v:
        raise ValueError("unknown venture")
    bmap = (v.get("brief") or {}).get("business_map")
    if not bmap or not bmap.get("aspects"):
        return {"nodes": [], "edges": [], "pending": True,
                "can_generate": discover_done(vid),
                "message": ("Your business map is generated once the Discover stage finishes. "
                            "Run Discover from the Overview to build it.")}

    nodes = [{"data": {"id": "center", "label": _cut(v.get("name") or "Your venture", 40),
                       "type": "center", "items": _actions(v, bmap)}}]
    edges = []
    for i, a in enumerate(bmap["aspects"]):
        nid = f"a{i}"
        details = [a.get("summary", "")] + list(a.get("points") or [])
        nodes.append({"data": {"id": nid, "label": _cut(a["label"], ASPECT_MAX), "type": "aspect",
                               "status": a.get("status", "partial"), "full_label": a["label"],
                               "items": [d for d in details if d]}})
        edges.append({"data": {"source": "center", "target": nid}})
        cid = f"{nid}c0"
        nodes.append({"data": {"id": cid, "label": _cut(a.get("summary", ""), LEAF_MAX),
                               "full": a.get("summary", ""), "type": "leaf"}})
        edges.append({"data": {"source": nid, "target": cid}})

    return {"nodes": nodes, "edges": edges, "pending": False,
            "aspect_count": len(bmap["aspects"]), "generated_at": bmap.get("generated_at")}
