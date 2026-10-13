"""OpenAI chat client for the answer step. Settings come from the environment, or from a .env
file at the project root (variables already set in the environment win):

  OPENAI_API_KEY           required for a model call (not for --dry-run)
  OPENAI_BASE_URL          default https://api.openai.com/v1
  RA_LLM_MODEL             default gpt-5.6-luna
  RA_LLM_REASONING_EFFORT  default none; sent as reasoning_effort (empty: not sent, for models
                           without the parameter)
  RA_LLM_MAX_OUTPUT        default 4000   max_completion_tokens
  RA_LLM_INPUT_TOKENS      default 24000  prompt budget; evidence is admitted up to it
  RA_LLM_TIMEOUT           default 120    seconds
  RA_LLM_RETRIES           default 2      SDK retries of a failed request
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from ra_ingest.settings import ROOT


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:  # an empty OPENAI_BASE_URL would make the SDK connect to ""
            os.environ.setdefault(key.strip(), value)


@dataclass
class LLMSettings:
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5.6-luna"
    reasoning_effort: str | None = "none"
    max_output: int = 4000
    input_tokens: int = 24000
    timeout: float = 120.0
    retries: int = 2

    @classmethod
    def from_env(cls) -> "LLMSettings":
        load_env()
        e = os.environ.get
        d = cls()
        return cls(api_key=e("OPENAI_API_KEY", ""), base_url=e("OPENAI_BASE_URL") or d.base_url,
                   model=e("RA_LLM_MODEL") or d.model,
                   reasoning_effort=e("RA_LLM_REASONING_EFFORT", d.reasoning_effort) or None,
                   max_output=int(e("RA_LLM_MAX_OUTPUT") or d.max_output),
                   input_tokens=int(e("RA_LLM_INPUT_TOKENS") or d.input_tokens),
                   timeout=float(e("RA_LLM_TIMEOUT") or d.timeout), retries=int(e("RA_LLM_RETRIES") or d.retries))


_WIDE = re.compile(r"[ᄀ-ᇿ　-鿿가-힯]")


class Tokens:
    """Prompt tokens: tiktoken o200k_base when installed, otherwise an estimate (one token per
    Hangul/CJK character, one per 4 other characters) that errs on the high side."""

    def __init__(self):
        try:
            import tiktoken

            self._enc = tiktoken.get_encoding("o200k_base")
            self.basis = "tiktoken:o200k_base"
        except Exception:  # not installed, or the encoding file cannot be downloaded
            self._enc = None
            self.basis = "estimate"

    def count(self, text: str) -> int:
        if self._enc is not None:
            return len(self._enc.encode(text, disallowed_special=()))
        wide = len(_WIDE.findall(text))
        return wide + (len(text) - wide + 3) // 4


class OpenAIChat:
    provider = "openai"

    def __init__(self, settings: LLMSettings | None = None, sdk=None, tokens: Tokens | None = None):
        self.s = settings or LLMSettings.from_env()
        self.model, self.input_tokens = self.s.model, self.s.input_tokens
        self.tokens = tokens or Tokens()
        self._sdk = sdk
        self.calls: list[dict] = []

    def count(self, text: str) -> int:
        return self.tokens.count(text)

    def count_messages(self, messages: list[dict]) -> int:
        return sum(self.count(m["content"]) + 4 for m in messages) + 8

    @property
    def sdk(self):
        if self._sdk is None:
            if not self.s.api_key:
                raise SystemExit(f"OPENAI_API_KEY is empty: set it in the environment or in {ROOT / '.env'} "
                                 "(see .env.example)")
            from openai import OpenAI

            self._sdk = OpenAI(api_key=self.s.api_key, base_url=self.s.base_url, timeout=self.s.timeout,
                               max_retries=self.s.retries)
        return self._sdk

    def __call__(self, messages: list[dict], json_schema: dict | None = None) -> dict:
        """One chat completion. json_schema={"name":..., "schema":...} forces a strict JSON reply.
        Returns the reply text with the model snapshot and token usage the server reported."""
        payload = {"model": self.model, "messages": messages, "max_completion_tokens": self.s.max_output}
        if self.s.reasoning_effort:
            payload["reasoning_effort"] = self.s.reasoning_effort
        if json_schema:
            payload["response_format"] = {"type": "json_schema", "json_schema": {**json_schema, "strict": True}}
        call = {"provider": self.provider, "model_requested": self.model,
                "reasoning_effort": self.s.reasoning_effort, "schema": (json_schema or {}).get("name"),
                "max_completion_tokens": self.s.max_output,
                "estimated_prompt_tokens": self.count_messages(messages), "token_count_basis": self.tokens.basis}
        self.calls.append(call)
        t0 = time.monotonic()
        try:
            r = self.sdk.chat.completions.create(**payload)
        except Exception as exc:
            call.update(status="error", error=f"{type(exc).__name__}: {exc}", seconds=round(time.monotonic() - t0, 3))
            raise
        ch, usage = r.choices[0], r.usage
        text = ch.message.content or ""
        call.update(seconds=round(time.monotonic() - t0, 3), model_reported=r.model,
                    system_fingerprint=getattr(r, "system_fingerprint", None), finish_reason=ch.finish_reason,
                    prompt_tokens=usage.prompt_tokens if usage else None,
                    completion_tokens=usage.completion_tokens if usage else None)
        refusal = getattr(ch.message, "refusal", None)
        if refusal:
            call["status"] = "refused"
            raise RuntimeError(f"the model refused: {refusal}")
        if ch.finish_reason != "stop" or not text:
            call["status"] = "incomplete"
            raise RuntimeError(f"incomplete reply (finish_reason={ch.finish_reason})"
                               + ("; raise RA_LLM_MAX_OUTPUT" if ch.finish_reason == "length" else ""))
        call["status"] = "completed"
        return {**call, "text": text}
