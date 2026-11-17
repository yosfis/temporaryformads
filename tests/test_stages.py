"""Validate / Build / Operate produce real outputs + a decision & summary every stage."""
import json
import re

import pytest

FEATURES = [{"name": "Upload Notes", "purpose": "add notes", "acceptance": "note saved"},
            {"name": "Quiz Me", "purpose": "practice", "acceptance": "quiz shown"},
            {"name": "Progress & Streaks", "purpose": "motivation", "acceptance": "streak shown"}]

GOOD_HTML = ("<!DOCTYPE html><html><head><title>StudyBuddy</title></head><body>"
             + "".join(f'<section data-feature="f{i + 1}"><h2>{f["name"]}</h2><button>Go</button></section>'
                       for i, f in enumerate(FEATURES))
             + "<script>" + "const notes=[];function addNote(t){notes.push(t);localStorage.setItem('n',JSON.stringify(notes));}\n" * 20
             + "</script></body></html>")


class Stub:
    model = "stub"

    def __init__(self, html=GOOD_HTML):
        self.html = html

    def ensure_ready(self):
        pass

    def chat(self, messages, **kw):
        if isinstance(self.html, Exception):
            raise self.html
        return "Here you go:\n```html\n" + self.html + "\n```"

    def chat_json(self, messages, *, system, schema_hint=None, temperature=0.2):
        h = schema_hint or ""
        if h == '{"summary": "string"}':
            return {"summary": "The stage_result looks solid overall."}  # snake_case on purpose
        if '"assessments"' in h:
            return {"assessments": [
                        {"verdict": "supported", "reasoning": "Survey shows demand.", "source_url": "https://ex.com/a"},
                        {"verdict": "supported", "reasoning": "No citation.", "source_url": ""},
                        {"verdict": "unverified", "reasoning": "Nothing found."}],
                    "experiments": [{"name": "Landing page test", "assumption": "x", "method": "ads",
                                     "success_metric": "5% signup", "cost": "$50", "time": "5 days"}],
                    "interview_questions": ["Tell me about the last time you studied for an exam."]}
        if '"features"' in h:
            return {"features": FEATURES, "out_of_scope": ["Mobile app"]}
        if '"kpis"' in h:
            return {"kpis": [{"metric": "Weekly active students", "target_90_days": "200", "how_to_measure": "analytics"}],
                    "roadmap": {"days_0_30": ["Recruit 20 beta users"], "days_31_60": ["Launch"], "days_61_90": ["Scale"]},
                    "risks": [{"risk": "Low retention", "mitigation": "Streaks"}],
                    "weekly_rhythm": ["Monday metrics review"], "first_action_tomorrow": "Email 10 students"}
        return {}


@pytest.fixture
def env(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from backend import app as app_mod
    from backend import orchestrator as orch_mod
    from tools import web_search
    stub = Stub()
    for agent in vars(app_mod.orchestrator).values():
        if hasattr(agent, "client"):
            monkeypatch.setattr(agent, "client", stub)
    monkeypatch.setattr(orch_mod, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(app_mod, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(web_search, "search", lambda q, max_results=6: {
        "results": [{"title": "t", "url": "https://ex.com/a", "snippet": "s"}], "error": None, "query_used": q})
    with TestClient(app_mod.app) as c:
        yield c, stub, tmp_path


def _venture_at(stage):
    from memory import database
    from config import STAGES
    v = database.create_venture(name="StudyBuddy", idea="AI study companion for students")
    brief = {"name": "StudyBuddy", "assumptions": ["Students will pay", "Exams are stressful", "Teachers share notes"]}
    database.update_venture(v["id"], brief=brief, current_stage=stage)
    for s in STAGES[:STAGES.index(stage)]:
        database.set_stage_status(v["id"], s, "completed")
    return v["id"]


def _report(vid, stage):
    from services import stage_report
    return stage_report.get(vid, stage)


def test_validate_tests_assumptions_and_writes_plan(env):
    c, _, out = env
    vid = _venture_at("validate")
    res = c.post(f"/api/ventures/{vid}/run-stage/validate").json()
    assert res["stage_status"] == "awaiting_approval"
    r = _report(vid, "validate")
    hl = {h["label"]: h["value"] for h in r["highlights"]}
    assert hl["Assumptions tested"] == "3"
    assert hl["Supported"] == "1"  # the uncited "supported" is downgraded to unverified
    assert r["decision"] == "GO" and r["decision_text"].startswith("Go ahead")
    assert "stage result" in r["summary"] and "_" not in r["summary"]  # plain English
    plan = (out / vid / "validation" / "validation_plan.md").read_text()
    assert "Landing page test" in plan and "interview" in plan.lower()
    assert c.get(f"/api/ventures/{vid}/artifacts/validation-plan").status_code == 200
    appr = c.get(f"/api/ventures/{vid}").json()["approvals"][0]
    assert appr["payload"]["decision_text"] and appr["payload"]["stage_label"] == "Validate"


def test_build_creates_working_prototype(env):
    c, _, out = env
    vid = _venture_at("build")
    c.post(f"/api/ventures/{vid}/run-stage/build")
    r = _report(vid, "build")
    assert r["decision"] == "GO", r
    assert any(l["url"].endswith("/artifacts/prototype") for l in r["links"])
    page = c.get(f"/api/ventures/{vid}/artifacts/prototype")
    assert page.status_code == 200 and 'data-feature="f3"' in page.text
    assert "connect-src 'none'" in page.headers["content-security-policy"]


def test_build_falls_back_to_template_when_ai_fails(env):
    c, stub, out = env
    stub.html = RuntimeError("timeout")
    vid = _venture_at("build")
    c.post(f"/api/ventures/{vid}/run-stage/build")
    r = _report(vid, "build")
    assert r["decision"] == "GO", r          # fallback still passes every check
    html = (out / vid / "mvp" / "index.html").read_text()
    assert "Progress &amp; Streaks" in html and "localStorage" in html
    assert all(re.search(rf'data-feature="f{i}"', html) for i in (1, 2, 3))


def test_final_stage_builds_report_and_finishes(env):
    c, _, out = env
    vid = _venture_at("operate")
    res = c.post(f"/api/ventures/{vid}/run-stage/operate").json()
    report_html = c.get(f"/api/ventures/{vid}/artifacts/report")
    assert report_html.status_code == 200 and "Weekly active students" in report_html.text
    fin = c.post("/api/approve", json={"venture_id": vid, "approval_id": res["approval_id"]}).json()
    assert fin["finished"] is True
    assert c.get(f"/api/ventures/{vid}").json()["stages"]["operate"]["status"] == "completed"


def test_every_stage_logs_plain_english_decision(env):
    c, _, _ = env
    vid = _venture_at("validate")
    c.post(f"/api/ventures/{vid}/run-stage/validate")
    d = c.get(f"/api/ventures/{vid}/decisions").json()[0]
    assert "_" not in d["action"] and "_" not in d["rationale"]


def test_rename_survives_brief_redraft(env):
    c, _, _ = env
    from memory import database
    vid = _venture_at("discover")
    assert c.post(f"/api/ventures/{vid}/rename", json={"name": "  My   Cool Startup "}).json()["name"] == "My Cool Startup"
    assert c.post(f"/api/ventures/{vid}/rename", json={"name": "   "}).status_code == 400
    c.post(f"/api/ventures/{vid}/redraft-brief")  # stub returns no name → must keep ours
    assert database.get_venture(vid)["name"] == "My Cool Startup"


def test_artifacts_reject_unknown_names(env):
    c, _, _ = env
    vid = _venture_at("discover")
    assert c.get(f"/api/ventures/{vid}/artifacts/..%2F..%2Fconfig.py").status_code == 404
    assert c.get(f"/api/ventures/{vid}/artifacts/prototype").status_code == 404  # not built yet
