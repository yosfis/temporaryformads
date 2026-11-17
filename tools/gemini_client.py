"""Gemini free-tier client."""
from __future__ import annotations
import json
import time
import os
from typing import Any
import httpx
from tools.ollama_client import OllamaError

NL = chr(10)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "60"))
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "2"))
GEMINI_TEMPERATURE = float(os.getenv("GEMINI_TEMPERATURE", "0.7"))


class GeminiClient:
    def __init__(self, api_key=GEMINI_API_KEY, model=GEMINI_MODEL, timeout=GEMINI_TIMEOUT):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.host = "https://generativelanguage.googleapis.com"

    def list_models(self):
        return [self.model]

    def ensure_ready(self):
        if not self.api_key:
            raise OllamaError(
                "GEMINI_API_KEY is not set. Get a free key at "
                "https://aistudio.google.com/app/apikey"
            )
        self._request("Reply with the word ok", system=None, temperature=0, json_mode=False)

    def _request(self, prompt, *, system, temperature, json_mode, num_predict=None, contents=None, timeout=None):
        # Key goes in a header, not the URL, so it can't leak into error messages/logs.
        url = self.host + "/v1beta/models/" + self.model + ":generateContent"
        headers = {"x-goog-api-key": self.api_key}
        payload = {
            "contents": contents or [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        if num_predict:
            payload["generationConfig"]["maxOutputTokens"] = num_predict
        last_err = None
        for _ in range(GEMINI_MAX_RETRIES + 1):
            try:
                r = httpx.post(url, json=payload, headers=headers, timeout=timeout or self.timeout)
                if r.status_code in (429, 503):
                    # Rate limit / overload: back off and retry (free tier hits this
                    # easily when Discover runs 4 research calls in parallel).
                    if _ < GEMINI_MAX_RETRIES:
                        time.sleep(min(2 ** (_ + 1), 20))
                        continue
                    if r.status_code == 429:
                        raise OllamaError("Gemini rate limit hit (429). Wait ~60s and retry.")
                    raise OllamaError(
                        "Gemini 503 UNAVAILABLE (model overloaded). "
                        "Try another GEMINI_MODEL, or wait 30-60s and retry."
                    )
                if r.status_code == 400:
                    raise OllamaError("Gemini 400: " + r.text[:300])
                if r.status_code == 403:
                    raise OllamaError("Gemini 403 key rejected: " + r.text[:200])
                if r.status_code >= 400:
                    raise OllamaError(f"Gemini HTTP {r.status_code}: " + r.text[:200])
                data = r.json()
                cands = data.get("candidates") or []
                if not cands:
                    reason = data.get("promptFeedback", {}).get("blockReason", "no candidates")
                    raise OllamaError("Gemini no output: " + str(reason))
                parts = cands[0].get("content", {}).get("parts") or []
                if not parts:
                    raise OllamaError("Gemini empty parts")
                return "".join(p.get("text", "") for p in parts)
            except OllamaError:
                raise
            except Exception as e:
                last_err = e
        raise OllamaError("Gemini failed: " + str(last_err))

    def chat(self, messages, *, system=None, temperature=GEMINI_TEMPERATURE, json_mode=False, num_predict=None, timeout=None):
        if not messages:
            raise OllamaError("No messages")
        # Preserve turn structure: Gemini uses "user"/"model" roles. Merge consecutive
        # same-role turns (Gemini rejects them) and any "system" message into system.
        contents = []
        for m in messages:
            role = m.get("role", "user")
            text = m.get("content", "") or ""
            if role == "system":
                system = (system + NL + NL + text) if system else text
                continue
            role = "model" if role == "assistant" else "user"
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"][0]["text"] += NL + NL + text
            else:
                contents.append({"role": role, "parts": [{"text": text}]})
        if not contents or contents[0]["role"] != "user":
            contents.insert(0, {"role": "user", "parts": [{"text": "(conversation start)"}]})
        return self._request(None, system=system, temperature=temperature, json_mode=json_mode,
                             num_predict=num_predict, contents=contents, timeout=timeout)

    def chat_json(self, messages, *, system, schema_hint=None, temperature=0.2):
        sys = system.strip()
        if schema_hint:
            sys = sys + " Return ONLY JSON matching: " + schema_hint
        else:
            sys = sys + " Return ONLY valid JSON."
        raw = ""
        for _ in range(GEMINI_MAX_RETRIES + 1):
            raw = self.chat(messages, system=sys, temperature=temperature, json_mode=True)
            parsed = _extract_json(raw)
            if parsed is not None:
                return parsed
        raise OllamaError("Gemini invalid JSON: " + raw[:300])


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(text[start:end+1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None
