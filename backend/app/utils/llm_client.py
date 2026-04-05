"""
LLM Client Wrapper
Unified OpenAI format API calls
Supports Ollama num_ctx parameter to prevent prompt truncation
"""

import json
import os
import re
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse
from openai import OpenAI

from ..config import Config


def _repair_json_loose(s: str) -> str:
    """Strip trailing commas before } or ] (common LLM JSON glitches)."""
    s = s.strip()
    s = re.sub(r",\s*}", "}", s)
    s = re.sub(r",\s*]", "]", s)
    return s


def _extract_top_level_json_object(s: str) -> Optional[str]:
    """Find first `{` … `}` slice with string-aware brace matching."""
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    quote = ""
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
        else:
            if c in "\"'":
                in_str = True
                quote = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
    return None


class LLMClient:
    """LLM Client"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        num_ctx: Optional[int] = None,
    ):
        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = base_url or Config.LLM_BASE_URL
        self.model = model or Config.LLM_MODEL_NAME

        if not self.api_key:
            raise ValueError("LLM_API_KEY not configured")

        effective_timeout = timeout if timeout is not None else Config.LLM_HTTP_TIMEOUT_SEC
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=effective_timeout,
        )

        # Ollama context window — pass explicit num_ctx for extract-only clients (Phase 9).
        if num_ctx is not None:
            self._num_ctx = num_ctx
        else:
            self._num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))

        # Set after each successful chat.completions.create (Phase 8 ingest metrics).
        self.last_usage: Optional[Dict[str, Optional[int]]] = None
        self.last_finish_reason: Optional[str] = None

    def _is_ollama(self) -> bool:
        """
        True when the backend should send Ollama-specific options (e.g. num_ctx).

        Uses LLM_PROVIDER=ollama to force, or URL heuristics (default port 11434).
        LM Studio and other OpenAI-compatible servers on other ports stay False unless forced.
        """
        provider = (os.environ.get('LLM_PROVIDER') or '').strip().lower()
        if provider == 'ollama':
            return True
        if provider in ('openai', 'lm_studio', 'lmstudio', 'vllm', 'custom'):
            return False

        base = self.base_url or ''
        if '11434' in base:
            return True
        parsed = urlparse(base if base.startswith(('http://', 'https://')) else f'http://{base}')
        return parsed.port == 11434

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> str:
        """
        Send chat request

        Args:
            messages: Message list
            temperature: Temperature parameter
            max_tokens: Max token count
            response_format: Response format (e.g., JSON mode)

        Returns:
            Model response text
        """
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if response_format:
            kwargs["response_format"] = response_format

        # For Ollama: pass num_ctx via extra_body to prevent prompt truncation
        if self._is_ollama() and self._num_ctx:
            kwargs["extra_body"] = {
                "options": {"num_ctx": self._num_ctx}
            }

        self.last_usage = None
        self.last_finish_reason = None
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as first_err:
            # LM Studio and some OpenAI-compatible servers reject response_format json_object
            if response_format and "response_format" in kwargs:
                kwargs_retry = {k: v for k, v in kwargs.items() if k != "response_format"}
                try:
                    response = self.client.chat.completions.create(**kwargs_retry)
                except Exception:
                    raise first_err from None
            else:
                raise first_err
        self._capture_response_meta(response)
        content = response.choices[0].message.content
        # Some models (like MiniMax M2.5) include <think>thinking content in response, need to remove
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        return content

    def _capture_response_meta(self, response: Any) -> None:
        """Store usage / finish_reason for ingest logging (best-effort)."""
        try:
            choice = response.choices[0]
            self.last_finish_reason = getattr(choice, "finish_reason", None)
        except (IndexError, AttributeError):
            self.last_finish_reason = None
        u = getattr(response, "usage", None)
        if u is None:
            self.last_usage = None
            return
        self.last_usage = {
            "prompt_tokens": getattr(u, "prompt_tokens", None),
            "completion_tokens": getattr(u, "completion_tokens", None),
            "total_tokens": getattr(u, "total_tokens", None),
        }

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """
        Send chat request and return JSON

        Args:
            messages: Message list
            temperature: Temperature parameter
            max_tokens: Max token count

        Returns:
            Parsed JSON object
        """
        response = self.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"}
        )
        # Clean markdown code block markers
        cleaned_response = response.strip()
        cleaned_response = re.sub(r'^```(?:json)?\s*\n?', '', cleaned_response, flags=re.IGNORECASE)
        cleaned_response = re.sub(r'\n?```\s*$', '', cleaned_response)
        cleaned_response = cleaned_response.strip()

        return self._parse_json_with_repair(cleaned_response)

    def _parse_json_with_repair(self, raw: str) -> Dict[str, Any]:
        """
        Parse model JSON; on failure try trailing-comma fix and substring extraction (Phase 9 TASK-027).
        """
        attempts: List[str] = [raw.strip(), _repair_json_loose(raw)]
        sub = _extract_top_level_json_object(raw)
        if sub and sub not in attempts:
            attempts.append(sub)
            attempts.append(_repair_json_loose(sub))

        last_err: Optional[Exception] = None
        for candidate in attempts:
            if not candidate:
                continue
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as e:
                last_err = e
                continue

        snippet = raw[:500] + ("…" if len(raw) > 500 else "")
        raise ValueError(f"Invalid JSON from LLM after repair attempts: {last_err}; snippet: {snippet}")
