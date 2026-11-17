"""Stage failure recovery, approval guards, and Gemini turn mapping."""
import pytest


def _new(idea="robustness"):
    from memory import database
    return database.create_venture(name="T", idea=idea)["id"]


def test_failed_stage_is_rerunnable(monkeypatch):
    from backend.orchestrator import Orchestrator
    from memory import database
    o = Orchestrator.__new__(Orchestrator)  # no agents/clients needed
    vid = _new()

    def boom(_vid):
        database.set_stage_status(_vid, "discover", "in_progress")
        raise RuntimeError("Gemini rate limit hit (429)")
    monkeypatch.setattr(o, "run_discover", boom)
    with pytest.raises(RuntimeError):
        o.run_stage(vid, "discover")
    assert database.get_stages(vid)["discover"]["status"] == "awaiting_input"


def test_stopped_stage_can_rerun(monkeypatch):
    from backend.orchestrator import Orchestrator
    from memory import database
    o = Orchestrator.__new__(Orchestrator)
    vid = _new()
    database.set_stage_status(vid, "discover", "stopped")
    monkeypatch.setattr(o, "run_discover", lambda _vid: {"ran": True})
    assert o.run_stage(vid, "discover") == {"ran": True}


def test_double_approve_does_not_skip_stage():
    from backend import approval_manager
    from backend.orchestrator import Orchestrator
    from memory import database
    o = Orchestrator.__new__(Orchestrator)
    vid = _new()
    database.set_stage_status(vid, "discover", "awaiting_approval")
    aid = approval_manager.request(vid, action="discover_gate", payload={})
    assert o.approve_and_advance(vid, aid)["advanced_to"] == "validate"
    with pytest.raises(ValueError):
        o.approve_and_advance(vid, aid)
    assert database.get_venture(vid)["current_stage"] == "validate"
    assert database.get_stages(vid)["validate"]["status"] == "not_started"


def test_gemini_keeps_roles_and_key_out_of_url(monkeypatch):
    from tools import gemini_client
    seen = {}

    class R:
        status_code = 200
        text = ""
        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "hi"}, {"text": "!"}]}}]}

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, payload=json, headers=headers)
        return R()
    monkeypatch.setattr(gemini_client.httpx, "post", fake_post)
    c = gemini_client.GeminiClient(api_key="SECRET", model="m")
    out = c.chat([{"role": "assistant", "content": "q1"}, {"role": "user", "content": "a1"},
                  {"role": "user", "content": "a2"}], system="sys")
    assert out == "hi!"
    assert "SECRET" not in seen["url"] and seen["headers"]["x-goog-api-key"] == "SECRET"
    roles = [c["role"] for c in seen["payload"]["contents"]]
    assert roles == ["user", "model", "user"]          # starts with user, no consecutive dupes
    assert "a1" in seen["payload"]["contents"][2]["parts"][0]["text"]
    assert "a2" in seen["payload"]["contents"][2]["parts"][0]["text"]
