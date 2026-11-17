"""Founder agent — clarification-first interview with explicit, stored state.

Interview state lives in `ventures.discovery_json`:
    {"answers": {topic: text}, "asked": {topic: n}, "pending": topic | None}

The topic being asked is recorded when the question is sent, so the founder's
next reply is attributed to that topic directly — no regex guessing about
which field a question "looked like". After every reply an extraction pass
reads the whole conversation and fills any *other* topics the founder already
mentioned, so we never re-ask something they told us.
"""
from __future__ import annotations
import json
from agents.base import BaseAgent

# Ordered: clarification first, then the founder-profile details.
TOPICS = {
    "problem": {
        "label": "Problem",
        "about": "the specific problem or pain point this solves, and why it matters",
        "fallback": "What specific problem does this solve, and why does it matter to the people who have it?",
    },
    "differentiation": {
        "label": "Unique angle",
        "about": "what makes this different from existing alternatives — their unique angle or unfair advantage",
        "fallback": "What makes this different from what already exists? What's your unique angle?",
    },
    "target_market": {
        "label": "Target market",
        "about": "who specifically their first customers or audience are",
        "fallback": "Who specifically is your first target customer or audience?",
    },
    "budget": {
        "label": "Budget",
        "about": "how much money they can realistically spend to get started",
        "fallback": "What budget can you realistically spend to get this off the ground?",
    },
    "skills": {
        "label": "Skills",
        "about": "the practical skills and hands-on experience they personally bring",
        "fallback": "What skills or hands-on experience do you personally bring to this venture?",
    },
    "education": {
        "label": "Education",
        "about": "their formal education or training (degree, field, institution, or self-taught)",
        "fallback": "What's your education — degree, field of study, or are you self-taught?",
    },
}
REQUIRED_FIELDS = ["budget", "skills", "education", "target_market"]
MAX_QUESTIONS = 10
MAX_ASKS_PER_TOPIC = 2  # re-ask once if the reply was empty/unclear, never more


def empty_state():
    return {"answers": {}, "asked": {}, "pending": None}


def normalize_state(state):
    s = empty_state()
    if isinstance(state, dict):
        s["answers"] = {k: v for k, v in (state.get("answers") or {}).items()
                        if k in TOPICS and isinstance(v, str) and v.strip()}
        s["asked"] = {k: int(v) for k, v in (state.get("asked") or {}).items() if k in TOPICS}
        s["pending"] = state.get("pending") if state.get("pending") in TOPICS else None
    return s


def _transcript(idea, history):
    lines = [f"Founder's initial idea: {idea}"]
    for m in history:
        role = "Founder" if m.get("role") == "user" else "Interviewer"
        lines.append(f"{role}: {m.get('content', '')}")
    return "\n".join(lines)


def _is_substantive(text):
    t = (text or "").strip().lower()
    return len(t) >= 2 and t not in {"idk", "?", "no", "n/a", "na", "skip", "pass"}


class FounderAgent(BaseAgent):
    name = "founder_understanding"
    description = "Clarifies the idea first, then gathers required founder details."
    system_prompt = "You are a warm, curious product partner interviewing a founder. Ask ONE question at a time."

    def run(self, context):
        return self.next_question(context.get("idea", ""), context.get("history", []),
                                  context.get("state"))

    # ── Interview loop ────────────────────────────────────────────────────
    def next_question(self, idea, history, state=None, *, new_message=None):
        """Advance the interview. Returns a response dict including the new `state`.

        `new_message` is the founder's latest reply (already in `history`).
        """
        state = normalize_state(state)
        answers = state["answers"]

        # 1) Attribute the reply to the topic we actually asked about.
        pending = state["pending"]
        if pending and new_message is not None and _is_substantive(new_message):
            prev = answers.get(pending)
            answers[pending] = f"{prev}\n{new_message.strip()}" if prev else new_message.strip()
        state["pending"] = None

        # 2) Register context: pull any other topics the founder already covered.
        missing = [t for t in TOPICS if t not in answers]
        if missing and (history or idea):
            try:
                extracted = self._extract(idea, history, missing)
                for t, val in extracted.items():
                    answers.setdefault(t, val)
            except Exception:
                pass  # interview continues on direct answers alone

        missing = [t for t in TOPICS if t not in answers]
        total_asked = sum(state["asked"].values())
        askable = [t for t in missing if state["asked"].get(t, 0) < MAX_ASKS_PER_TOPIC]

        base = {"missing_fields": [f for f in REQUIRED_FIELDS if f not in answers],
                "answered_fields": sorted(answers)}

        if not askable or total_asked >= MAX_QUESTIONS:
            return {**base, "question": "", "ready_for_brief": True, "phase": "ready", "state": state}

        target = askable[0]
        try:
            question = self._compose_question(target, idea, history, answers)
        except Exception:
            question = TOPICS[target]["fallback"]
        state["asked"][target] = state["asked"].get(target, 0) + 1
        state["pending"] = target
        phase = "clarify" if target in ("problem", "differentiation") else "details"
        return {**base, "question": question, "ready_for_brief": False, "phase": phase, "state": state}

    def _extract(self, idea, history, topics):
        """Return {topic: text} for topics the founder has *explicitly* covered."""
        wanted = {t: TOPICS[t]["about"] for t in topics}
        schema = "{" + ", ".join(f'"{t}": "string or null"' for t in topics) + "}"
        result = self._chat_json({
            "transcript": _transcript(idea, history),
            "topics": wanted,
            "instruction": (
                "For each topic, if the FOUNDER (not the interviewer) has already stated it anywhere "
                "in the transcript, return a short faithful summary in their words. "
                "If they have not clearly stated it, return null. Do NOT guess or infer."
            ),
        }, schema_hint=schema)
        out = {}
        for t in topics:
            val = result.get(t)
            if isinstance(val, str) and val.strip() and val.strip().lower() not in ("null", "none", "unknown", "not mentioned", "not specified"):
                out[t] = val.strip()
        return out

    def _compose_question(self, topic, idea, history, answers):
        recent = history[-6:]
        result = self._chat_json({
            "venture_idea": idea,
            "what_we_already_know": {TOPICS[k]["label"]: v for k, v in answers.items()},
            "recent_conversation": [{"role": m.get("role"), "content": m.get("content", "")} for m in recent],
            "ask_about": TOPICS[topic]["about"],
            "instruction": (
                "Write the interviewer's next message: ONE short, natural question (max 30 words) about "
                "`ask_about`. You may open with a brief acknowledgement (a few words) of the founder's "
                "last reply. Never re-ask anything listed in `what_we_already_know`. "
                "Never repeat a question already in the conversation word-for-word — if the founder's "
                "previous reply to this topic was unclear, rephrase more concretely."
            ),
        }, schema_hint='{"question": "string"}')
        q = str(result.get("question", "")).strip().strip('"').strip()
        previous = {(m.get("content") or "").strip() for m in history if m.get("role") == "assistant"}
        if not q or q in previous or "?" not in q:
            return TOPICS[topic]["fallback"]
        return q

    # ── Workspace helpers ────────────────────────────────────────────────
    def answer_question(self, venture, question, history=None):
        history = history or []
        brief = dict(venture.get("brief") or {})
        brief.pop("business_map", None)  # keep the prompt lean
        answers = normalize_state(venture.get("discovery"))["answers"]
        system = (
            "You are a helpful, direct business advisor. "
            "Answer the user's question concisely (2-4 sentences) and specifically. "
            "Use the venture context when relevant; answer general questions generally.\n\n"
            f"Venture idea: {venture.get('idea') or ''}\n"
            f"Founder interview answers: {json.dumps(answers, ensure_ascii=False)}\n"
            f"Business brief: {json.dumps(brief, ensure_ascii=False)}\n"
        )
        messages = []
        for h in history[-10:]:
            role = h.get("role", "user")
            if role in ("user", "assistant") and h.get("content"):
                messages.append({"role": role, "content": h.get("content", "")})
        # Ollama/Gemini both expect the conversation to start with the user.
        while messages and messages[0]["role"] != "user":
            messages.pop(0)
        messages.append({"role": "user", "content": question})
        try:
            return self.client.chat(messages, system=system)
        except Exception as e:
            raise RuntimeError(f"Q&A failed: {e}")

    def analyze_answers(self, answers):
        answers = {k: answers.get(k, "") for k in REQUIRED_FIELDS}
        schema = '{"budget": "string", "skills": "string", "education": "string", "target_market": "string"}'
        return self._chat_json({
            "raw_answers": answers,
            "instruction": (
                "Normalize the founder's raw answers into clean, specific, human-readable phrases. "
                "Do NOT invent facts. Return the SAME four keys.\n"
                "- budget: '<comma-number> <CURRENCY CODE>'. 'like a million dollars' -> '1,000,000 USD'. "
                "'maybe 5000 EGP' -> '5,000 EGP'. Empty -> 'Not specified'.\n"
                "- skills: '<level> experience in <domain>'. Levels: Minimal, Some, Moderate, Extensive, Expert. "
                "Empty -> 'Not specified'.\n"
                "- education: '<degree> in <field> at <institution>' (bachelor -> BSc/BEng/BA, master -> MSc/MA/MBA, phd -> PhD). "
                "'self taught' -> 'Self-taught'. Empty -> 'Not specified'.\n"
                "- target_market: concise audience description, title-cased. Empty -> 'Not specified'.\n"
                "Return only the JSON object."
            ),
        }, schema_hint=schema)

    def draft_brief(self, idea, history, answers=None):
        schema = (
            '{"name": "string", "category": "technology|education|sustainability|creative|productivity|general", '
            '"concept": "string", "problem": "string", "solution": "string", '
            '"target_audience": "string", "business_model": "string", '
            '"budget": "string", "founder_skills": ["string"], "founder_education": "string", '
            '"theme": {"accent": "#RRGGBB", "accent_2": "#RRGGBB", '
            '"background": "#RRGGBB (DARK)", "surface": "#RRGGBB (DARK)", "mood": "string"}, '
            '"assumptions": ["string"], "constraints": ["string"], "open_questions": ["string"]}'
        )
        return self._chat_json({
            "idea": idea,
            "founder_answers": {TOPICS[k]["label"]: v for k, v in (answers or {}).items() if k in TOPICS},
            "conversation": history,
            "instruction": "Draft the Business Brief. Base it on founder_answers and the conversation; quote founder answers verbatim. Never invent facts. For theme: DARK MODE ONLY. background must be #050508 to #141420. surface slightly lighter. accent vibrant. FORBIDDEN: luminance above 20%.",
        }, schema_hint=schema)
