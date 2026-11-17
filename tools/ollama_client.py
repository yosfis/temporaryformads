"""Ollama client with MODEL_PROVIDER dispatch."""
from __future__ import annotations
import json, os
from typing import Any
import httpx
from config import (OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_MAX_RETRIES,
                    OLLAMA_TEMPERATURE, OLLAMA_TIMEOUT)


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, host=OLLAMA_HOST, model=OLLAMA_MODEL, timeout=OLLAMA_TIMEOUT):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout

    def list_models(self) -> list[str]:
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=5)
            r.raise_for_status()
        except Exception as e:
            raise OllamaError(
                f"Cannot reach Ollama at {self.host}. Is it running? "
                f"Start with `ollama serve`. ({e})"
            ) from e
        return [m["name"] for m in r.json().get("models", [])]

    def ensure_ready(self) -> None:
        if not self.model:
            raise OllamaError(
                "OLLAMA_MODEL not set. 1) ollama list  2) add OLLAMA_MODEL to .env"
            )
        models = self.list_models()
        if not any(m == self.model or m.split(":")[0] == self.model.split(":")[0]
                   for m in models):
            raise OllamaError(
                f"Model '{self.model}' not in Ollama.\n"
                f"Available: {models or '(none)'}\n"
                f"Install: ollama pull {self.model}"
            )

    def chat(self, messages, *, system=None, temperature=OLLAMA_TEMPERATURE,
             json_mode=False, num_predict=None, timeout=None) -> str:
        if system:
            messages = [{"role": "system", "content": system}, *messages]
        payload: dict[str, Any] = {
            "model": self.model, "messages": messages, "stream": False,
            "options": {"temperature": temperature},
        }
        if json_mode:
            payload["format"] = "json"
        if num_predict:
            payload["options"]["num_predict"] = num_predict
        last_err = None
        for _ in range(OLLAMA_MAX_RETRIES + 1):
            try:
                r = httpx.post(f"{self.host}/api/chat", json=payload, timeout=timeout or self.timeout)
                r.raise_for_status()
                return r.json()["message"]["content"]
            except Exception as e:
                last_err = e
        raise OllamaError(f"Ollama chat failed: {last_err}")

    def chat_json(self, messages, *, system, schema_hint=None, temperature=0.2):
        sys = system.strip()
        if schema_hint:
            sys += f"\n\nReturn ONLY valid JSON matching:\n{schema_hint}"
        else:
            sys += "\n\nReturn ONLY valid JSON."
        raw = ""
        for _ in range(OLLAMA_MAX_RETRIES + 1):
            raw = self.chat(messages, system=sys, temperature=temperature, json_mode=True)
            parsed = _extract_json(raw)
            if parsed is not None:
                return parsed
        raise OllamaError(f"Invalid JSON after retries: {raw[:300]!r}")


def _extract_json(text: str):
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
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


_default_client = None


def get_client():
    """Return the active client based on MODEL_PROVIDER. No silent fallback."""
    global _default_client
    if _default_client is not None:
        return _default_client

    provider = os.getenv("MODEL_PROVIDER", "ollama").lower().strip()
    print(f"[VentureOS] get_client() -> MODEL_PROVIDER={provider!r}", flush=True)

    if provider == "gemini":
        key = os.getenv("GEMINI_API_KEY", "").strip()
        if not key:
            raise OllamaError(
                "MODEL_PROVIDER=gemini but GEMINI_API_KEY is empty.\n"
                "1) Get a free key at https://aistudio.google.com/app/apikey\n"
                "2) Add GEMINI_API_KEY=AIza... to .env\n"
                "3) Restart: python main.py\n"
                "OR set MODEL_PROVIDER=ollama in .env to use local Qwen."
            )
        # Google issues two formats: "AIza..." (legacy, 39 chars)
        # and "AQ.Ab8..." (new, ~53 chars). Accept anything >= 20 chars
        # without whitespace; let Google's API reject it if truly bad.
        if not key:
            raise OllamaError(
                f"MODEL_PROVIDER=gemini but GEMINI_API_KEY looks wrong "
                f"(length={len(key)}, starts={key[:6]!r}). "
                f"Get a fresh key at https://aistudio.google.com/app/apikey"
            )
        try:
            from tools.gemini_client import GeminiClient
        except ImportError as e:
            raise OllamaError(
                f"MODEL_PROVIDER=gemini but tools/gemini_client.py "
                f"failed to import: {e}"
            ) from e
        _default_client = GeminiClient()
        print(
            f"[VentureOS] Using GeminiClient (model={_default_client.model})",
            flush=True,
        )
    else:
        _default_client = OllamaClient()
        print(
            f"[VentureOS] Using OllamaClient (model={_default_client.model})",
            flush=True,
        )

    return _default_client
