"""Orchestrator — six stages with checkpoints + decision logging."""
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
from backend import approval_manager
from backend.state_manager import StateManager
from config import OUTPUTS_DIR, STAGES
from memory import database
from memory.database import connect
from memory.schemas import utcnow
from services import decision_log, evidence_ledger


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


class Orchestrator:
    def __init__(self):
        self.founder = FounderAgent()
        self.research = ResearchAgent()
        self.feasibility = FeasibilityAgent()
        self.strategy = StrategyAgent()
        self.development = DevelopmentAgent()
        self.testing = TestingAgent()
        self.commercial = CommercialAgent()
        self.financial = FinancialAgent()
        self.marketing = MarketingAgent()
        self.operations = OperationsAgent()
        self.case_research = CaseResearchAgent()
        self.business_map = BusinessMapAgent()

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

    def run_discover(self, vid):
        v = database.get_venture(vid)
        if not v: raise ValueError("unknown venture")
        _clear_checkpoints(vid, "discover")
        StateManager.set_stage(vid, "discover", "in_progress")
        _checkpoint(vid, "discover", "Starting 4 research topics in parallel…")

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
                    found = len(result.get("sources", []))
                    findings_count = len(result.get("findings", []))
                    _checkpoint(vid, "discover", f"Topic {i + 1}/4 complete — {found} sources",
                                extra={"topic_index": i + 1, "question": q,
                                       "sources_count": found, "findings_count": findings_count})
                except Exception as e:
                    results[i] = {"sources": [], "findings": [], "limitations": [str(e)], "query_used": q}
                    _checkpoint(vid, "discover", f"Topic {i + 1}/4 failed — {str(e)[:80]}",
                                extra={"topic_index": i + 1, "error": str(e)[:400]})

        research_results = [results.get(i, {"sources": [], "findings": []}) for i in range(len(questions))]
        total_sources = sum(len(r.get("sources", [])) for r in research_results)
        total_findings = sum(len(r.get("findings", [])) for r in research_results)
        _checkpoint(vid, "discover", f"Aggregate: {total_sources} sources, {total_findings} findings",
                    extra={"total_sources": total_sources, "total_findings": total_findings})

        _checkpoint(vid, "discover", "Assessing feasibility across 5 dimensions…")
        feas = self.feasibility.run({"venture": v, "stage": "discover"})
        gate = feas.get("gate_recommendation", "REPEAT")
        _checkpoint(vid, "discover", f"Verdict: {feas.get('classification')} -> {gate}",
                    extra={"classification": feas.get("classification"), "gate": gate,
                           "dimensions": feas.get("dimensions", {})})

        database.update_venture(vid, gate=gate, gate_rationale=feas.get("rationale", ""))
        decision_log.log(vid, stage="discover", agent="feasibility",
                         action="feasibility_gate", decision=gate,
                         rationale=feas.get("rationale", "")[:500])

        feedback = {
            "sources_found": total_sources,
            "findings_extracted": total_findings,
            "dimensions": feas.get("dimensions", {}),
            "risks": feas.get("risks", []),
            "next_steps": feas.get("next_steps", []),
            "sources_used": feas.get("sources_used", []),
            "research_per_topic": [
                {"question": q, "findings": len(r.get("findings", [])), "sources": len(r.get("sources", []))}
                for q, r in zip(questions, research_results)],
        }

        _checkpoint(vid, "discover", "Building business map (12 core aspects)…")
        try:
            bmap = self.build_business_map(vid, feasibility=feas)
            gaps = sum(1 for a in bmap["aspects"] if a["status"] == "gap")
            _checkpoint(vid, "discover", f"Business map ready — {len(bmap['aspects'])} aspects, {gaps} gaps")
        except Exception as e:
            _checkpoint(vid, "discover", f"Business map failed — {str(e)[:80]}")

        if gate == "STOP":
            StateManager.set_stage(vid, "discover", "stopped")
            _checkpoint(vid, "discover", "STOP — venture not feasible as described")
            return {"feasibility": feas, "feedback": feedback, "stage_status": "stopped",
                    "warning": "The agents found this venture is NOT feasible."}

        StateManager.set_stage(vid, "discover", "awaiting_approval")
        _checkpoint(vid, "discover", "Awaiting your approval")
        approval_id = approval_manager.request(vid, action="discover_gate",
            payload={"recommendation": gate, "rationale": feas.get("rationale", ""),
                     "classification": feas.get("classification"), "sources_used": feas.get("sources_used", [])})
        return {"feasibility": feas, "feedback": feedback, "stage_status": "awaiting_approval",
                "approval_id": approval_id}

    def run_validate(self, vid):
        v = database.get_venture(vid)
        if not v: raise ValueError("unknown venture")
        _clear_checkpoints(vid, "validate")
        StateManager.set_stage(vid, "validate", "in_progress")
        _checkpoint(vid, "validate", "Thinking of validation experiments…")

        brief = v.get("brief") or {}
        assumptions = brief.get("assumptions") or []
        prompt = (f"Venture: {v['idea']}\nBrief: {brief}\nAssumptions to test: {assumptions}\n"
                  "Design 3 lean validation experiments. Return JSON.")
        _checkpoint(vid, "validate", "Drafting experiments…")
        try:
            experiments = self.research.client.chat_json(
                messages=[{"role": "user", "content": prompt}],
                system="You are a lean startup validation expert. Be specific and cheap.",
                schema_hint=('{"experiments":[{"name":"string","hypothesis":"string","method":"string",'
                             '"success":"string","failure":"string","cost":"string","time":"string"}]}'))
        except Exception as e:
            experiments = {"experiments": [], "error": str(e)}

        exps = experiments.get("experiments", [])
        _checkpoint(vid, "validate", f"Generated {len(exps)} experiments",
                    extra={"count": len(exps), "names": [e.get("name") for e in exps[:5]]})
        for exp in exps:
            evidence_ledger.add(vid, type="assumption",
                                claim=f"Experiment: {exp.get('name','?')} — {exp.get('hypothesis','?')}",
                                details=f"Method: {exp.get('method','')}",
                                agent="validation", stage="validate")

        n = len(exps)
        gate = "GO" if n >= 2 else "REPEAT"
        rationale = f"{n} validation experiments designed."
        database.update_venture(vid, gate=gate, gate_rationale=rationale)
        decision_log.log(vid, stage="validate", agent="validation",
                         action="experiment_design", decision=gate, rationale=rationale)
        StateManager.set_stage(vid, "validate", "awaiting_approval")
        _checkpoint(vid, "validate", "Awaiting your approval")
        approval_id = approval_manager.request(vid, action="validate_gate",
            payload={"recommendation": gate, "rationale": rationale, "classification": f"{n} experiments"})
        return {"experiments": exps, "stage_status": "awaiting_approval", "approval_id": approval_id,
                "feedback": {"experiments_designed": n}}

    def run_build(self, vid):
        v = database.get_venture(vid)
        if not v: raise ValueError("unknown venture")
        _clear_checkpoints(vid, "build")
        StateManager.set_stage(vid, "build", "in_progress")
        _checkpoint(vid, "build", "Designing MVP specification…")

        evidence = evidence_ledger.list_for(vid)
        spec = self.strategy.generate_mvp_spec(v, evidence)
        features = spec.get("features", [])
        _checkpoint(vid, "build", f"Defined {len(features)} MVP features",
                    extra={"features": [f.get("name") for f in features[:6]]})

        _checkpoint(vid, "build", "Generating application scaffold…")
        out_dir = OUTPUTS_DIR / vid / "mvp"
        scaffold = self.development.scaffold_mvp(v, spec, out_dir)
        _checkpoint(vid, "build", f"Wrote {len(scaffold['files'])} files",
                    extra={"files": scaffold.get("files", []), "output_dir": scaffold.get("output_dir")})

        _checkpoint(vid, "build", "Running tests…")
        test_result = self.testing.generate_smoke_test(v, spec, out_dir)
        _checkpoint(vid, "build", f"Tests {'passed' if test_result.get('passed') else 'FAILED'}",
                    extra={"passed": test_result.get("passed"), "output": (test_result.get("output") or "")[:400]})

        gate = "GO" if len(features) >= 3 and test_result.get("passed") else "REPEAT"
        rationale = (f"Generated {len(features)} features. Files: {len(scaffold['files'])}. "
                     f"Tests {'passed' if test_result.get('passed') else 'failed'}.")
        database.update_venture(vid, gate=gate, gate_rationale=rationale)
        decision_log.log(vid, stage="build", agent="development", action="mvp_scaffold",
                         decision=gate, rationale=rationale)
        StateManager.set_stage(vid, "build", "awaiting_approval")
        _checkpoint(vid, "build", "Awaiting your approval")
        approval_id = approval_manager.request(vid, action="build_gate",
            payload={"recommendation": gate, "rationale": rationale,
                     "classification": f"{len(features)} features, {len(scaffold['files'])} files"})
        return {"spec": spec, "files": scaffold["files"], "test": test_result,
                "output_dir": scaffold["output_dir"], "stage_status": "awaiting_approval",
                "approval_id": approval_id,
                "feedback": {"features_generated": len(features), "files_written": len(scaffold["files"]),
                             "tests_passed": test_result.get("passed"), "output_location": scaffold["output_dir"]}}

    def run_commercial(self, vid):
        v = database.get_venture(vid)
        if not v: raise ValueError("unknown venture")
        _clear_checkpoints(vid, "commercial")
        StateManager.set_stage(vid, "commercial", "in_progress")
        _checkpoint(vid, "commercial", "Preparing pricing strategy…")

        evidence = evidence_ledger.list_for(vid)
        pricing = self.commercial.prepare_pricing(v, evidence)
        _checkpoint(vid, "commercial", f"Pricing model: {pricing.get('model')}",
                    extra={"model": pricing.get("model"), "price": pricing.get("price_per_unit"),
                           "unit": pricing.get("unit"), "confidence": pricing.get("confidence")})

        _checkpoint(vid, "commercial", "Calculating unit economics…")
        economics = self.financial.model_unit_economics(v, pricing)
        _checkpoint(vid, "commercial",
                    f"Margin {economics.get('gross_margin_pct')}%, break-even {economics.get('break_even_units')} units",
                    extra={"gross_margin_pct": economics.get("gross_margin_pct"),
                           "break_even_units": economics.get("break_even_units"),
                           "contribution_margin": economics.get("contribution_margin")})

        gate = "GO" if economics.get("gross_margin_pct", 0) > 20 else "REPEAT"
        rationale = (f"Pricing: {pricing.get('model','?')}. Margin: {economics.get('gross_margin_pct',0)}%. "
                     f"Break-even: {economics.get('break_even_units','N/A')}.")
        database.update_venture(vid, gate=gate, gate_rationale=rationale)
        decision_log.log(vid, stage="commercial", agent="financial",
                         action="unit_economics", decision=gate, rationale=rationale)
        StateManager.set_stage(vid, "commercial", "awaiting_approval")
        _checkpoint(vid, "commercial", "Awaiting your approval")
        approval_id = approval_manager.request(vid, action="commercial_gate",
            payload={"recommendation": gate, "rationale": rationale,
                     "classification": f"margin {economics.get('gross_margin_pct')}%"})
        return {"pricing": pricing, "economics": economics, "stage_status": "awaiting_approval",
                "approval_id": approval_id,
                "feedback": {"model": pricing.get("model"), "price": pricing.get("price_per_unit"),
                             "gross_margin_pct": economics.get("gross_margin_pct"),
                             "break_even_units": economics.get("break_even_units"),
                             "formulas": economics.get("formulas")}}

    def run_launch(self, vid):
        v = database.get_venture(vid)
        if not v: raise ValueError("unknown venture")
        _clear_checkpoints(vid, "launch")
        StateManager.set_stage(vid, "launch", "in_progress")
        _checkpoint(vid, "launch", "Generating marketing copy…")

        evidence = evidence_ledger.list_for(vid)
        materials = self.marketing.generate_launch_materials(v, evidence)
        _checkpoint(vid, "launch", f"Generated tagline + {len(materials.get('claims', []))} claims",
                    extra={"tagline": materials.get("tagline"), "claims_total": len(materials.get("claims", []))})

        unsupported = materials.get("unsupported_claims", [])
        _checkpoint(vid, "launch", f"{len(unsupported)} claims need evidence verification",
                    extra={"unsupported_claims": unsupported[:10]})

        gate = "GO" if len(unsupported) == 0 else "REPEAT"
        rationale = f"Tagline + {len(materials.get('claims', []))} claims. {len(unsupported)} unsupported."
        database.update_venture(vid, gate=gate, gate_rationale=rationale)
        decision_log.log(vid, stage="launch", agent="marketing", action="launch_claims",
                         decision=gate, rationale=rationale)
        StateManager.set_stage(vid, "launch", "awaiting_approval")
        _checkpoint(vid, "launch", "Awaiting your approval")
        approval_id = approval_manager.request(vid, action="launch_gate",
            payload={"recommendation": gate, "rationale": rationale,
                     "classification": f"{len(unsupported)} unsupported", "unsupported_claims": unsupported})
        return {"materials": materials, "unsupported_claims": unsupported, "stage_status": "awaiting_approval",
                "approval_id": approval_id,
                "feedback": {"tagline": materials.get("tagline"), "claims_total": len(materials.get("claims", [])),
                             "claims_unsupported": len(unsupported)}}

    def run_operate(self, vid):
        v = database.get_venture(vid)
        if not v: raise ValueError("unknown venture")
        _clear_checkpoints(vid, "operate")
        StateManager.set_stage(vid, "operate", "in_progress")
        _checkpoint(vid, "operate", "Analyzing business metrics…")

        metrics = v.get("metrics") or {}
        has_metrics = bool(metrics)
        if not has_metrics:
            metrics = {"note": "no metrics provided"}

        analysis = self.operations.analyze(v, metrics)
        _checkpoint(vid, "operate",
                    f"Issues: {len(analysis.get('issues', []))}, Recommendations: {len(analysis.get('recommendations', []))}",
                    extra={"issues": analysis.get("issues", [])[:5], "recommendations": analysis.get("recommendations", [])[:5]})

        cases = {"cases": []}
        if analysis.get("issues"):
            _checkpoint(vid, "operate", "Searching comparable historical cases…")
            first_issue = analysis["issues"][0].get("observation", "")
            cases = self.case_research.find_similar_cases(v, first_issue[:100])
            _checkpoint(vid, "operate", f"Found {len(cases.get('cases', []))} historical cases",
                        extra={"cases": [c.get("company") for c in cases.get("cases", [])[:5]]})

        gate = "GO"
        rationale = f"Issues: {len(analysis.get('issues', []))}. Cases: {len(cases.get('cases', []))}."
        database.update_venture(vid, gate=gate, gate_rationale=rationale)
        decision_log.log(vid, stage="operate", agent="operations", action="ops_analysis",
                         decision=gate, rationale=rationale)
        StateManager.set_stage(vid, "operate", "awaiting_approval")
        _checkpoint(vid, "operate", "Awaiting your approval")
        approval_id = approval_manager.request(vid, action="operate_gate",
            payload={"recommendation": gate, "rationale": rationale, "classification": "ongoing operations"})
        return {"analysis": analysis, "cases": cases.get("cases", []), "stage_status": "awaiting_approval",
                "approval_id": approval_id,
                "feedback": {"has_real_metrics": has_metrics, "issues": len(analysis.get("issues", [])),
                             "recommendations": len(analysis.get("recommendations", [])),
                             "cases_found": len(cases.get("cases", []))}}

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
        if current_status in ("in_progress", "awaiting_approval", "completed", "stopped"):
            return {"status": "already_ran",
                    "message": f"Stage '{stage}' is '{current_status}'. Approve or reject the pending decision first."}
        if current_status == "awaiting_input":
            with database.connect() as c:
                c.execute("DELETE FROM evidence WHERE venture_id=? AND stage=?", (vid, stage))
                c.execute("DELETE FROM approvals WHERE venture_id=? AND status='pending'", (vid,))
        dispatch = {"discover": self.run_discover, "validate": self.run_validate,
                    "build": self.run_build, "commercial": self.run_commercial,
                    "launch": self.run_launch, "operate": self.run_operate}
        return dispatch[stage](vid)

    def approve_and_advance(self, vid, approval_id):
        v = database.get_venture(vid)
        if not v:
            raise ValueError("unknown venture")
        approval_manager.resolve(approval_id, "approved")
        StateManager.set_stage(vid, v["current_stage"], "completed")
        decision_log.log(vid, stage=v["current_stage"], agent="founder",
                         action="approve_gate", decision="APPROVED",
                         rationale="Founder approved.", approval_status="approved")
        return {"advanced_to": StateManager.advance_stage(vid)}

    def reject(self, vid, approval_id):
        approval_manager.resolve(approval_id, "rejected")
        v = database.get_venture(vid)
        if v:
            StateManager.set_stage(vid, v["current_stage"], "awaiting_input")
            decision_log.log(vid, stage=v["current_stage"], agent="founder",
                             action="reject_gate", decision="REJECTED",
                             rationale="Founder rejected.", approval_status="rejected")
        return {"ok": True}

    def stop(self, vid):
        v = database.get_venture(vid)
        if v:
            StateManager.set_stage(vid, v["current_stage"], "stopped")
