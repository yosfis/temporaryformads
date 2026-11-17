"""Interview flow + business map, driven through the API with a stub LLM."""
import json

import pytest


class StubClient:
    """Deterministic stand-in for Ollama/Gemini.

    - extraction calls: reports `target_market` as known (it's in the idea), nothing else
    - question composition: returns a fixed question that mentions 'background'
      (the phrasing that used to be misclassified and loop forever)
    - business map: returns only 3 aspects to exercise the 12-aspect backfill
    """
    model = "stub"

    def ensure_ready(self):
        pass

    def chat(self, messages, *, system=None, **kw):
        return "stub answer"

    def chat_json(self, messages, *, system, schema_hint=None, temperature=0.2):
        payload = json.loads(messages[-1]["content"])
        if "topics" in payload:  # extraction
            return {t: ("university students" if t == "target_market" else None) for t in payload["topics"]}
        if "ask_about" in payload:  # compose
            return {"question": f"Tell me about your background regarding {payload['ask_about'][:30]}?"}
        if "aspects" in payload:  # business map
            return {"aspects": {"problem": {"summary": "Students waste time", "points": ["p1"], "status": "strong"},
                                "market": {"summary": "Large", "points": [], "status": "partial"},
                                "risks": {"summary": "Competition", "points": [], "status": "bogus"}},
                    "next_actions": ["Interview 10 students"]}
        if "raw_answers" in payload:
            return {k: v for k, v in payload["raw_answers"].items()}
        return {"name": "StudyBuddy", "category": "education", "concept": "c", "problem": "p",
                "solution": "s", "target_audience": "students", "business_model": "subscription",
                "assumptions": ["a1"], "open_questions": []}


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from backend import app as app_mod
    stub = StubClient()
    for agent in vars(app_mod.orchestrator).values():
        if hasattr(agent, "client"):
            monkeypatch.setattr(agent, "client", stub)
    with TestClient(app_mod.app) as c:
        yield c


def test_interview_never_repeats_and_registers_context(client):
    idea = "A study planner app for university students"
    v = client.post("/api/ventures", json={"idea": idea}).json()
    res = client.post("/api/founder/next-question", json={"venture_id": v["id"], "message": idea}).json()
    asked = []
    replies = ["Students miss deadlines", "AI scheduling", "5000 USD", "I code in Python", "BSc Computer Science"]
    for reply in replies:
        assert not res["ready_for_brief"], res
        asked.append(res["question"])
        res = client.post("/api/founder/next-question", json={"venture_id": v["id"], "message": reply}).json()

    assert res["ready_for_brief"], res
    assert len(asked) == len(set(asked)), f"repeated a question: {asked}"
    # target_market came from the idea itself → never asked
    assert not any("first customers" in q for q in asked)

    answers = client.get(f"/api/ventures/{v['id']}/summary").json()["answers"]
    assert answers["education"] == "BSc Computer Science"
    assert answers["skills"] == "I code in Python"
    assert answers["budget"] == "5000 USD"
    assert answers["target_market"] == "university students"


def test_business_map_only_after_discover_and_has_12_aspects(client):
    from memory import database
    from backend import app as app_mod
    v = client.post("/api/ventures", json={"idea": "x"}).json()
    database.update_venture(v["id"], brief={"name": "X", "target_audience": "students"})

    mm = client.get(f"/api/ventures/{v['id']}/mindmap").json()
    assert mm["pending"] and not mm["can_generate"]
    assert client.post(f"/api/ventures/{v['id']}/mindmap/generate").status_code == 409

    database.set_stage_status(v["id"], "discover", "awaiting_approval")
    mm = client.post(f"/api/ventures/{v['id']}/mindmap/generate").json()
    aspects = [n for n in mm["nodes"] if n["data"]["type"] == "aspect"]
    assert len(aspects) == 12
    by_label = {n["data"]["full_label"]: n["data"] for n in aspects}
    assert by_label["Problem"]["status"] == "strong"
    assert by_label["Target Customers"]["items"][0] == "students"   # backfilled from brief
    assert by_label["Risks & Regulation"]["status"] == "partial"     # invalid status normalised
    # brief re-draft must not wipe the map
    assert database.get_venture(v["id"])["brief"]["business_map"]["aspects"]
