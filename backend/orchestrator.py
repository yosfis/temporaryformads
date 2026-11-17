"""Orchestrator — six gated stages. Every stage ends the same way:
decision -> plain-English summary -> persisted report -> founder approval."""
from __future__ import annotations
import json
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed

from agents.business_map_agent import BusinessMapAgent
from agents.case_research_agent import CaseResearchAgent
from agents.commercial_agent import CommercialAgent
from agents.development_agent import DevelopmentAgent
from agents.feasibility_agent import FeasibilityAgent
from agents.financial_agent import FinancialAgent
from agents.founder_agent import FounderAgent
from agents.marketing_agent import MarketingAgent
from agents.operations_agent import OperationsAgent
from agents.research_agent import ResearchAgent
from agents.strategy_agent import StrategyAgent
from agents.testing_agent import TestingAgent
from agents.validation_agent import ValidationAgent
from backend import approval_manager
from backend.state_manager import StateManager
from config import OUTPUTS_DIR, STAGES
from memory import database
from memory.database import connect
from memory.schemas import utcnow
from services import decision_log, evidence_ledger, stage_report, venture_report

# Downloadable outputs, by public name -> path inside outputs/<venture id>/
ARTIFACTS = {
    "prototype": "mvp/index.html",
    "validation-plan": "validation/validation_plan.md",
    "report": "final/venture_report.html",
}


def artifact_url(vid, name):
    return f"/api/ventures/{vid}/artifacts/{name}"


def _clear_checkpoints(vid, stage):
    with connect() as c:
        c.execute("UPDATE stage_state SET notes_json='{}', updated_at=? WHERE venture_id=? AND stage=?",
                  (utcnow(), vid, stage))


def _checkpoint(vid, stage, message, extra=None):
    for attempt in range(5):
        try:
            with connect() as c:
                row = c.execute("SELECT notes_json FROM stage_state WHERE venture_id=? AND stage=?",
                                (vid, stage)).fetchone()
                notes = {}
                if row and row["notes_json"]:
                    try:
                        notes = json.loads(row["notes_json"]) or {}
                    except Exception:
                        notes = {}
                cps = notes.get("checkpoints", [])
                entry = {"time": utcnow(), "message": message}
                if extra is not None:
                    entry["extra"] = extra
                cps.append(entry)
                notes["checkpoints"] = cps
                c.execute("UPDATE stage_state SET notes_json=?, updated_at=? WHERE venture_id=? AND stage=?",
                          (json.dumps(notes), utcnow(), vid, stage))
            return
        except Exception as e:
            if "locked" in str(e).lower() and attempt < 4:
                _time.sleep(0.1 * (attempt + 1))
                continue
            raise


def recover_interrupted_stages():
    """On startup nothing can be running — unstick stages a crash/restart left 'in_progress'."""
    with connect() as c:
        c.execute("UPDATE stage_state SET status='awaiting_input', updated_at=? WHERE status='in_progress'",
                  (utcnow(),))


class Orchestrator:
    def __init__(self):
        self.founder = FounderAgent()
        self.research = ResearchAgent()
        self.feasibility = FeasibilityAgent()
        self.validation = ValidationAgent()
        self.strategy = StrategyAgent()
        self.development = DevelopmentAgent()
        self.testing = TestingAgent()
        self.commercial = CommercialAgent()
        self.financial = FinancialAgent()
        self.marketing = MarketingAgent()
        self.operations = OperationsAgent()
        self.case_research = CaseResearchAgent()
        self.business_map = BusinessMapAgent()

    # ── Shared ───────────────────────────────────────────────────────────
    def build_business_map(self, vid, feasibility=None):
        """Generate the 12-aspect business map and store it on the brief."""
        v = database.get_venture(vid)
        if not v:
            raise ValueError("unknown venture")
        bmap = self.business_map.generate(v, feasibility=feasibility)
        bmap["generated_at"] = utcnow()
        fresh = database.get_venture(vid)  # re-read so concurrent brief edits aren't clobbered
        brief = fresh.get("brief") or {}
        brief["business_map"] = bmap
        database.update_venture(vid, brief=brief)
        return bmap

    def _start(self, vid, stage, first_message):
        v = database.get_venture(vid)
        if not v:
            raise ValueError("unknown venture")
        _clear_checkpoints(vid, stage)
        StateManager.set_stage(vid, stage, "in_progress")
        _checkpoint(vid, stage, first_message)
        return v

    def _finish(self, vid, stage, *, gate, rationale, agent, action, facts, fallback_summary,
                highlights=None, sections=None, links=None, extra=None):
        """Decision + summary + report + approval — identical for every stage."""
        _checkpoint(vid, stage, "Writing the stage summary…")
        summary = stage_report.summarize(self.founder.client, stage, gate, facts, fallback_summary)
        report = stage_report.build(stage, gate, summary, highlights=highlights,
                                    sections=sections, links=links)
        stage_report.save(vid, stage, report)
        database.update_venture(vid, gate=gate, gate_rationale=summary)
        decision_log.log(vid, stage=stage, agent=agent, action=action, decision=gate,
                         rationale=summary[:800])
        status = "stopped" if gate == "STOP" else "awaiting_approval"
        StateManager.set_stage(vid, stage, status)
        _checkpoint(vid, stage, "Decision ready — awaiting your approval")
        approval_id = approval_manager.request(vid, action=f"{stage}_gate", payload={
            "recommendation": gate, "decision_text": report["decision_text"],
            "rationale": summary, "stage_label": report["stage_label"], **(extra or {})})
        return {"stage_status": status, "approval_id": approval_id, "report": report}

    # ── 1. Discover ──────────────────────────────────────────────────────
    def run_discover(self, vid):
        v = self._start(vid, "discover", "Starting 4 research topics in parallel…")
        idea = v["idea"][:180]
        questions = [
            f"Competitors and pricing for {idea}",
            f"Customer pain points for {idea}",
            f"Market size and growth for {idea}",
            f"Barriers and regulations for {idea}",
        ]
        results = {}

        def _one(idx, q):
            return idx, self.research.run({"venture": v, "stage": "discover", "question": q})

        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(_one, i, q): (i, q) for i, q in enumerate(questions)}
            for fut in as_completed(futures):
                i, q = futures[fut]
                try:
                    _, result = fut.result()
                    results[i] = result
                    _checkpoint(vid, "discover", f"Topic {i + 1}/4 complete — {len(result.get('sources', []))} sources")
                except Exception as e:
                    results[i] = {"sources": [], "findings": []}
                    _checkpoint(vid, "discover", f"Topic {i + 1}/4 failed — {str(e)[:80]}")

        research = [results.get(i, {"sources": [], "findings": []}) for i in range(len(questions))]
        total_sources = sum(len(r.get("sources", [])) for r in research)
        total_findings = sum(len(r.get("findings", [])) for r in research)
        _checkpoint(vid, "discover", f"Found {total_sources} sources and {total_findings} findings")

        _checkpoint(vid, "discover", "Assessing feasibility across 5 dimensions…")
        feas = self.feasibility.run({"venture": v, "stage": "discover"})
        gate = feas.get("gate_recommendation", "REPEAT")
        _checkpoint(vid, "discover", f"Verdict: {str(feas.get('classification', '')).title()}")

        _checkpoint(vid, "discover", "Building business map (12 core aspects)…")
        bmap = None
        try:
            bmap = self.build_business_map(vid, feasibility=feas)
            gaps = sum(1 for a in bmap["aspects"] if a["status"] == "gap")
            _checkpoint(vid, "discover", f"Business map ready — 12 aspects, {gaps} gaps")
        except Exception as e:
            _checkpoint(vid, "discover", f"Business map failed — {str(e)[:80]}")

        dims = feas.get("dimensions") or {}
        out = self._finish(
            vid, "discover", gate=gate, rationale=feas.get("rationale", ""), agent="feasibility",
            action="Feasibility decision",
            facts={"classification": feas.get("classification"), "rationale": feas.get("rationale"),
                   "dimensions": dims, "risks": feas.get("risks"), "sources": total_sources},
            fallback_summary=feas.get("rationale") or f"Research found {total_findings} findings.",
            highlights=[("Verdict", str(feas.get("classification", "—")).title()),
                        ("Sources found", total_sources), ("Findings", total_findings)],
            sections=[
                ("Feasibility by dimension", [f"{k.title()}: {str(d.get('score', '?')).title()} — {d.get('reason', '')}"
                                              for k, d in dims.items() if isinstance(d, dict)]),
                ("Business map — 12 core aspects", [f"{a['label']}: {a['summary']}"
                                                    for a in (bmap or {}).get("aspects", [])]),
                ("Key risks", feas.get("risks") or []),
                ("Recommended next steps", feas.get("next_steps") or []),
            ],
            links=[("Open the business mind map", "#mindmap")] if bmap else None,
            extra={"classification": feas.get("classification"), "sources_used": feas.get("sources_used", [])})
        return {**out, "feasibility": feas}

    # ── 2. Validate ──────────────────────────────────────────────────────
    def run_validate(self, vid):
        v = self._start(vid, "validate", "Picking the riskiest assumptions to test…")
        res = self.validation.validate(v, OUTPUTS_DIR / vid / "validation",
                                       on_progress=lambda m: _checkpoint(vid, "validate", m))
        a = res["assessments"]
        n_sup = sum(x["verdict"] == "supported" for x in a)
        n_con = sum(x["verdict"] == "contradicted" for x in a)
        n_unv = len(a) - n_sup - n_con
        _checkpoint(vid, "validate", f"{n_sup} supported, {n_con} contradicted, {n_unv} unverified")
        if not a:
            gate = "REPEAT"
        elif n_con * 2 >= len(a):
            gate = "PIVOT"
        elif n_con == 0 and n_sup >= 1:
            gate = "GO"
        else:
            gate = "REPEAT"
        icon = {"supported": "Supported", "contradicted": "Contradicted", "unverified": "Unverified"}
        return self._finish(
            vid, "validate", gate=gate, rationale="", agent="validation", action="Assumption validation",
            facts={"assessments": a, "experiments_planned": len(res["experiments"])},
            fallback_summary=(f"We tested {len(a)} key assumptions against web evidence: {n_sup} supported, "
                              f"{n_con} contradicted and {n_unv} still unverified. "
                              f"{len(res['experiments'])} cheap experiments are planned to close the gaps."),
            highlights=[("Assumptions tested", len(a)), ("Supported", n_sup), ("Contradicted", n_con),
                        ("Unverified", n_unv), ("Experiments planned", len(res["experiments"]))],
            sections=[
                ("Assumptions", [f"{icon[x['verdict']]} — {x['assumption']} ({x['reasoning']})" for x in a]),
                ("Experiments to run", [f"{e.get('name')}: {e.get('method', '')} Success = {e.get('success_metric', '')} "
                                        f"({e.get('cost', '?')}, {e.get('time', '?')})" for e in res["experiments"]]),
                ("Customer interview questions", res["interview_questions"]),
            ],
            links=[("Download validation plan & interview script", artifact_url(vid, "validation-plan"))])

    # ── 3. Build ─────────────────────────────────────────────────────────
    def run_build(self, vid):
        v = self._start(vid, "build", "Designing the MVP specification…")
        evidence = evidence_ledger.list_for(vid)
        spec = self.strategy.generate_mvp_spec(v, evidence)
        features = (spec.get("features") or [])[:5]
        _checkpoint(vid, "build", f"Defined {len(features)} MVP features")

        out_dir = OUTPUTS_DIR / vid / "mvp"
        build = self.development.scaffold_mvp(v, spec, out_dir,
                                              on_progress=lambda m: _checkpoint(vid, "build", m))
        _checkpoint(vid, "build", "Prototype built" + (" (template fallback)" if build["source"] != "ai" else ""))
        _checkpoint(vid, "build", "Running automated checks on the prototype…")
        test = self.testing.generate_smoke_test(v, spec, out_dir)
        _checkpoint(vid, "build", f"Checks: {test['output']}")

        gate = "GO" if test["passed"] and len(features) >= 3 else "REPEAT"
        return self._finish(
            vid, "build", gate=gate, rationale="", agent="development", action="MVP prototype build",
            facts={"features": [f.get("name") for f in features], "tests": test["output"],
                   "built_by": "AI" if build["source"] == "ai" else "template fallback"},
            fallback_summary=(f"Built a clickable prototype with {len(features)} MVP features. "
                              f"Automated checks: {test['output']}."),
            highlights=[("Features", len(features)), ("Checks", test["output"]),
                        ("Built by", "AI" if build["source"] == "ai" else "Template (AI generation failed)")],
            sections=[
                ("MVP features", [f"{f.get('name')}: {f.get('purpose', '')}" for f in features]),
                ("Automated checks", [f"{'Passed' if c['passed'] else 'Failed'} — {c['check']}"
                                      + (f" ({c['detail']})" if c["detail"] else "") for c in test["checks"]]),
                ("Out of scope for now", spec.get("out_of_scope") or []),
            ],
            links=[("Open the prototype", artifact_url(vid, "prototype"))])

    # ── 4. Commercial ────────────────────────────────────────────────────
    def run_commercial(self, vid):
        v = self._start(vid, "commercial", "Preparing pricing strategy…")
        evidence = evidence_ledger.list_for(vid)
        pricing = self.commercial.prepare_pricing(v, evidence)
        _checkpoint(vid, "commercial", f"Pricing model: {str(pricing.get('model', '?')).title()}")
        _checkpoint(vid, "commercial", "Calculating unit economics…")
        eco = self.financial.model_unit_economics(v, pricing)
        margin = eco.get("gross_margin_pct", 0)
        gate = "GO" if margin > 20 else "REPEAT"
        cur = eco.get("currency") or ""
        return self._finish(
            vid, "commercial", gate=gate, rationale="", agent="financial", action="Pricing and unit economics",
            facts={"pricing": pricing, "economics": {k: eco.get(k) for k in (
                "price_per_unit", "variable_cost_per_unit", "fixed_costs_monthly", "gross_margin_pct",
                "break_even_units")}},
            fallback_summary=(f"Recommended a {pricing.get('model', '?')} model at {pricing.get('price_per_unit', '?')} "
                              f"per {pricing.get('unit', 'unit')}. Gross margin is {margin}%, breaking even at "
                              f"{eco.get('break_even_units') or 'N/A'} units per month."),
            highlights=[("Pricing model", str(pricing.get("model", "—")).title()),
                        ("Price", f"{pricing.get('price_per_unit', '—')} per {pricing.get('unit', 'unit')}"),
                        ("Gross margin", f"{margin}%"),
                        ("Monthly fixed costs", f"{eco.get('fixed_costs_monthly')} {cur}".strip()),
                        ("Break-even", f"{eco.get('break_even_units') or 'N/A'} units / month"),
                        ("Confidence", str(pricing.get("confidence", "—")).title())],
            sections=[("Why this price", [pricing.get("rationale", "")]),
                      ("Competitor price anchors", pricing.get("competitor_anchors") or []),
                      ("Cost assumptions", eco.get("assumptions") or [])],
            extra={"classification": f"Gross margin {margin}%"})

    # ── 5. Launch ────────────────────────────────────────────────────────
    def run_launch(self, vid):
        v = self._start(vid, "launch", "Writing launch copy…")
        evidence = evidence_ledger.list_for(vid)
        m = self.marketing.generate_launch_materials(v, evidence)
        unsupported = m.get("unsupported_claims", [])
        supported = [c.get("text", "") for c in m.get("claims", []) if c.get("status") != "unsupported"]
        _checkpoint(vid, "launch", f"{len(m.get('claims', []))} marketing claims, {len(unsupported)} need evidence")
        gate = "GO" if not unsupported else "REPEAT"
        return self._finish(
            vid, "launch", gate=gate, rationale="", agent="marketing", action="Launch materials",
            facts={"tagline": m.get("tagline"), "claims": len(m.get("claims", [])), "unsupported": unsupported},
            fallback_summary=(f"Prepared launch copy with {len(m.get('claims', []))} marketing claims; "
                              f"{len(unsupported)} still need evidence before you can publish them."),
            highlights=[("Tagline", m.get("tagline")), ("One-liner", m.get("one_liner")),
                        ("Call to action", m.get("call_to_action"))],
            sections=[("Claims backed by evidence", supported),
                      ("Claims that need evidence first", unsupported)],
            extra={"unsupported_claims": unsupported})

    # ── 6. Operate (final) ───────────────────────────────────────────────
    def run_operate(self, vid):
        v = self._start(vid, "operate", "Pulling together everything learned so far…")
        context = {s: {k: r.get(k) for k in ("decision_text", "summary", "highlights")}
                   for s in STAGES if (r := stage_report.get(vid, s))}
        _checkpoint(vid, "operate", "Building the 90-day operating plan…")
        plan = self.operations.operating_plan(v, context)
        kpis, roadmap = plan.get("kpis") or [], plan.get("roadmap") or {}
        _checkpoint(vid, "operate", f"{len(kpis)} KPIs and a 30/60/90-day roadmap ready")

        cases = []
        risks = plan.get("risks") or []
        if risks:
            try:
                _checkpoint(vid, "operate", "Looking for comparable real-world cases…")
                cases = self.case_research.find_similar_cases(v, str(risks[0].get("risk", ""))[:100]).get("cases", [])
            except Exception:
                cases = []
        gate = "GO"
        out = self._finish(
            vid, "operate", gate=gate, rationale="", agent="operations", action="Operating plan",
            facts={"kpis": kpis, "first_action": plan.get("first_action_tomorrow")},
            fallback_summary=(f"Your 90-day operating plan is ready with {len(kpis)} KPIs and a phased roadmap. "
                              f"The full venture report compiles every stage into one document."),
            highlights=[("First action tomorrow", plan.get("first_action_tomorrow")), ("KPIs", len(kpis))],
            sections=[
                ("KPIs (90-day targets)", [f"{k.get('metric')}: {k.get('target_90_days')} — {k.get('how_to_measure', '')}"
                                           for k in kpis]),
                ("Days 0–30", roadmap.get("days_0_30") or []),
                ("Days 31–60", roadmap.get("days_31_60") or []),
                ("Days 61–90", roadmap.get("days_61_90") or []),
                ("Risks & mitigations", [f"{r.get('risk')} → {r.get('mitigation')}" for r in risks]),
                ("Weekly operating rhythm", plan.get("weekly_rhythm") or []),
                ("Comparable cases", [f"{c.get('company')}: {c.get('outcome')}" for c in cases]),
            ],
            links=[("Open the full venture report", artifact_url(vid, "report")),
                   ("Open the prototype", artifact_url(vid, "prototype"))])
        # Report is compiled after this stage's own report is saved, so it's included.
        venture_report.write(database.get_venture(vid), OUTPUTS_DIR / vid / ARTIFACTS["report"])
        return out

    # ── Control ──────────────────────────────────────────────────────────
    def run_stage(self, vid, stage):
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage}")
        v = database.get_venture(vid)
        if not v:
            raise ValueError("unknown venture")
        if v["current_stage"] != stage:
            return {"status": "wrong_stage",
                    "message": f"You are on '{v['current_stage']}', not '{stage}'."}
        stages = database.get_stages(vid)
        current_status = stages[stage]["status"]
        if current_status in ("in_progress", "awaiting_approval", "completed"):
            return {"status": "already_ran",
                    "message": f"This stage is {current_status.replace('_', ' ')}. Approve or reject the pending decision first."}
        if current_status in ("awaiting_input", "stopped"):
            # Re-run (after a rejection, a STOP verdict, or a failed run): start clean.
            with database.connect() as c:
                c.execute("DELETE FROM evidence WHERE venture_id=? AND stage=?", (vid, stage))
                c.execute("UPDATE approvals SET status='rejected', resolved_at=? "
                          "WHERE venture_id=? AND status='pending'", (utcnow(), vid))
        dispatch = {"discover": self.run_discover, "validate": self.run_validate,
                    "build": self.run_build, "commercial": self.run_commercial,
                    "launch": self.run_launch, "operate": self.run_operate}
        try:
            return dispatch[stage](vid)
        except Exception as e:
            # Never leave a stage stuck "in_progress" — make it re-runnable.
            StateManager.set_stage(vid, stage, "awaiting_input")
            _checkpoint(vid, stage, f"Failed — {str(e)[:120]}. You can run the stage again.")
            raise

    @staticmethod
    def _pending_approval(vid, approval_id):
        if not any(a["id"] == approval_id for a in approval_manager.list_pending(vid)):
            raise ValueError("This decision was already resolved or doesn't belong to this venture.")

    def approve_and_advance(self, vid, approval_id):
        v = database.get_venture(vid)
        if not v:
            raise ValueError("unknown venture")
        self._pending_approval(vid, approval_id)
        approval_manager.resolve(approval_id, "approved")
        StateManager.set_stage(vid, v["current_stage"], "completed")
        decision_log.log(vid, stage=v["current_stage"], agent="founder",
                         action="Founder approval", decision="APPROVED",
                         rationale="Founder approved the decision and moved on.", approval_status="approved")
        nxt = StateManager.advance_stage(vid)
        return {"advanced_to": nxt, "finished": v["current_stage"] == STAGES[-1]}

    def reject(self, vid, approval_id):
        self._pending_approval(vid, approval_id)
        approval_manager.resolve(approval_id, "rejected")
        v = database.get_venture(vid)
        if v:
            StateManager.set_stage(vid, v["current_stage"], "awaiting_input")
            decision_log.log(vid, stage=v["current_stage"], agent="founder",
                             action="Founder rejection", decision="REJECTED",
                             rationale="Founder rejected the decision; the stage can be run again.",
                             approval_status="rejected")
        return {"ok": True}

    def stop(self, vid):
        v = database.get_venture(vid)
        if v:
            StateManager.set_stage(vid, v["current_stage"], "stopped")
