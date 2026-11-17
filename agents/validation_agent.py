"""Validation agent — tests the riskiest assumptions against web evidence and
produces a concrete experiment plan + customer interview script."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agents.base import BaseAgent
from services import evidence_ledger
from tools import web_search

MAX_ASSUMPTIONS = 4
VERDICTS = ("supported", "contradicted", "unverified")


class ValidationAgent(BaseAgent):
    name = "validation"
    description = "Checks key assumptions against evidence and designs cheap experiments."
    system_prompt = ("You are a rigorous lean-startup validation analyst. Judge assumptions ONLY on the "
                     "search results given. If results don't clearly address an assumption, it is unverified.")

    def _assumptions(self, venture):
        brief = venture.get("brief") or {}
        items = [a for a in (brief.get("assumptions") or []) if str(a).strip()]
        if not items:
            bmap = brief.get("business_map") or {}
            items = [f"{a['label']}: {a['summary']}" for a in bmap.get("aspects", [])
                     if a.get("status") in ("partial", "gap")]
        if not items:
            out = self._chat_json({
                "venture_idea": venture.get("idea"), "brief": brief,
                "instruction": "List the 4 riskiest assumptions this venture depends on, one sentence each.",
            }, schema_hint='{"assumptions": ["string"]}')
            items = out.get("assumptions") or []
        return [str(a).strip() for a in items][:MAX_ASSUMPTIONS]

    def validate(self, venture, output_dir: Path, on_progress=None):
        idea = (venture.get("idea") or "")[:80]
        assumptions = self._assumptions(venture)
        if on_progress:
            on_progress(f"Testing {len(assumptions)} key assumptions against web evidence…")

        def _search(a):
            try:
                return web_search.search(f"{a[:120]} {idea}", max_results=4).get("results", [])
            except Exception:
                return []
        with ThreadPoolExecutor(max_workers=MAX_ASSUMPTIONS) as ex:
            results = list(ex.map(_search, assumptions))
        if on_progress:
            on_progress(f"Collected {sum(len(r) for r in results)} sources — assessing…")

        schema = ('{"assessments": [{"assumption": "string", "verdict": "supported|contradicted|unverified", '
                  '"reasoning": "string (1-2 sentences)", "source_url": "string or empty"}], '
                  '"experiments": [{"name": "string", "assumption": "string", "method": "string", '
                  '"success_metric": "string", "cost": "string", "time": "string"}], '
                  '"interview_questions": ["string"]}')
        out = self._chat_json({
            "venture_idea": venture.get("idea"),
            "assumptions_with_search_results": [{"assumption": a, "search_results": r}
                                                for a, r in zip(assumptions, results)],
            "instruction": ("1) Assess EVERY assumption (same order). 2) Design one cheap experiment (under a "
                            "week, low cost) for each assumption that is not 'supported'. 3) Write 6-8 "
                            "non-leading customer interview questions about the problem (not the solution)."),
        }, schema_hint=schema)

        assessments = []
        raw = out.get("assessments") or []
        for i, a in enumerate(assumptions):
            r = raw[i] if i < len(raw) and isinstance(raw[i], dict) else {}
            verdict = r.get("verdict") if r.get("verdict") in VERDICTS else "unverified"
            src = str(r.get("source_url") or "").strip()
            if verdict == "supported" and not src.startswith("http"):
                verdict = "unverified"  # no citation, no support
            assessments.append({"assumption": a, "verdict": verdict,
                                "reasoning": str(r.get("reasoning") or "No clear evidence found."),
                                "source_url": src if src.startswith("http") else ""})
            evidence_ledger.add(venture["id"],
                                type="verified" if verdict == "supported" else "assumption",
                                claim=f"Assumption {verdict}: {a}", source=assessments[-1]["source_url"] or None,
                                details=assessments[-1]["reasoning"], agent=self.name, stage="validate")

        experiments = [e for e in (out.get("experiments") or []) if isinstance(e, dict) and e.get("name")]
        for e in experiments:
            evidence_ledger.add(venture["id"], type="assumption",
                                claim=f"Experiment: {e.get('name')} — {e.get('success_metric', '')}",
                                details=e.get("method", ""), agent=self.name, stage="validate")
        questions = [str(q) for q in (out.get("interview_questions") or []) if str(q).strip()]

        output_dir.mkdir(parents=True, exist_ok=True)
        plan = output_dir / "validation_plan.md"
        plan.write_text(self._markdown(venture, assessments, experiments, questions), encoding="utf-8")
        return {"assessments": assessments, "experiments": experiments,
                "interview_questions": questions, "plan_file": str(plan)}

    @staticmethod
    def _markdown(venture, assessments, experiments, questions):
        icon = {"supported": "✅", "contradicted": "❌", "unverified": "❔"}
        md = [f"# Validation plan — {venture.get('name') or 'Venture'}", "", f"_Idea:_ {venture.get('idea')}", "",
              "## Assumptions tested", ""]
        for a in assessments:
            md.append(f"- {icon[a['verdict']]} **{a['verdict'].title()}** — {a['assumption']}  ")
            md.append(f"  {a['reasoning']}" + (f" ([source]({a['source_url']}))" if a["source_url"] else ""))
        md += ["", "## Experiments to run", ""]
        for e in experiments:
            md += [f"### {e.get('name')}", f"- **Tests:** {e.get('assumption', '')}",
                   f"- **Method:** {e.get('method', '')}", f"- **Success looks like:** {e.get('success_metric', '')}",
                   f"- **Cost / time:** {e.get('cost', '?')} / {e.get('time', '?')}", ""]
        md += ["## Customer interview script", ""] + [f"{i}. {q}" for i, q in enumerate(questions, 1)]
        return "\n".join(md) + "\n"
