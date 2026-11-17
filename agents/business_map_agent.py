"""Business map agent — synthesises the 12 core aspects after the Discover stage."""
from __future__ import annotations
from agents.base import BaseAgent
from services import evidence_ledger

# key -> (label, what it should cover). Order is the order shown on the map.
BUSINESS_ASPECTS = {
    "problem":           ("Problem", "the customer pain point and why it matters"),
    "solution":          ("Solution", "what the product/service is and how it solves the problem"),
    "customers":         ("Target Customers", "who the first customers are, segments, early adopters"),
    "value_proposition": ("Value Proposition", "why customers would choose this over alternatives"),
    "market":            ("Market Size", "market size, growth and demand signals"),
    "competition":       ("Competition", "direct/indirect competitors and how this is positioned"),
    "revenue":           ("Revenue Model", "how it makes money, pricing approach"),
    "costs":             ("Cost Structure", "main costs, startup budget, unit economics"),
    "channels":          ("Channels & Marketing", "how customers are reached and acquired"),
    "operations":        ("Operations & Resources", "key activities, resources, partners, tech needed"),
    "team":              ("Team & Founder Fit", "founder skills/education and gaps to fill"),
    "risks":             ("Risks & Regulation", "key risks, barriers, regulation, unvalidated assumptions"),
}
STATUSES = ("strong", "partial", "gap")


class BusinessMapAgent(BaseAgent):
    name = "business_map"
    description = "Builds the 12-aspect business map from the brief, interview and Discover research."
    system_prompt = (
        "You are a sharp startup analyst. Summarise a venture across fixed business aspects using ONLY "
        "the supplied brief, founder answers and research evidence. Never invent numbers or facts; "
        "if something is unknown, say so and mark it as a gap."
    )

    def generate(self, venture, *, feasibility=None):
        evidence = [e for e in evidence_ledger.list_for(venture["id"])
                    if e.get("stage") in (None, "discover")][:40]
        brief = dict(venture.get("brief") or {})
        brief.pop("business_map", None)
        brief.pop("theme", None)
        aspects_spec = {k: f"{label}: {about}" for k, (label, about) in BUSINESS_ASPECTS.items()}
        schema = ('{"aspects": {"<aspect_key>": {"summary": "string (one sentence)", '
                  '"points": ["string (max 3, concrete, <= 90 chars each)"], '
                  '"status": "strong|partial|gap"}}, '
                  '"next_actions": ["string (max 5, concrete)"]}')
        try:
            result = self._chat_json({
                "venture_idea": venture.get("idea"),
                "brief": brief,
                "founder_answers": (venture.get("discovery") or {}).get("answers", {}),
                "research_evidence": [{"type": e["type"], "claim": e["claim"], "source": e.get("source")}
                                      for e in evidence],
                "feasibility": feasibility or {"rationale": venture.get("gate_rationale", "")},
                "aspects": aspects_spec,
                "instruction": ("Return an entry for EVERY key in `aspects` (exactly these 12 keys). "
                                "status: strong = backed by evidence, partial = stated but unverified, "
                                "gap = unknown/missing."),
            }, schema_hint=schema)
        except Exception:
            result = {}
        return self._normalize(result, brief, venture, evidence)

    @staticmethod
    def _normalize(result, brief, venture, evidence):
        """Guarantee all 12 aspects exist, falling back to the brief where the model left gaps."""
        raw = result.get("aspects") if isinstance(result, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        answers = (venture.get("discovery") or {}).get("answers", {})
        fallback = {
            "problem": brief.get("problem") or answers.get("problem"),
            "solution": brief.get("solution") or brief.get("concept"),
            "customers": brief.get("target_audience") or answers.get("target_market"),
            "value_proposition": answers.get("differentiation"),
            "revenue": brief.get("business_model"),
            "costs": brief.get("budget") or answers.get("budget"),
            "team": ", ".join(brief.get("founder_skills") or []) or answers.get("skills"),
            "risks": "; ".join((brief.get("assumptions") or [])[:2]) or None,
        }
        aspects = []
        for key, (label, _about) in BUSINESS_ASPECTS.items():
            a = raw.get(key) if isinstance(raw.get(key), dict) else {}
            summary = str(a.get("summary") or "").strip()
            points = [str(p).strip() for p in (a.get("points") or []) if str(p).strip()][:3]
            status = a.get("status") if a.get("status") in STATUSES else None
            if not summary:
                fb = fallback.get(key)
                summary = str(fb).strip() if fb else "Not enough information yet."
                status = status or ("partial" if fb else "gap")
            aspects.append({"key": key, "label": label, "summary": summary,
                            "points": points, "status": status or "partial"})
        actions = [str(x).strip() for x in (result.get("next_actions") or [])
                   if str(x).strip()][:5] if isinstance(result, dict) else []
        return {"aspects": aspects, "next_actions": actions}
