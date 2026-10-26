"""
Unified LLM client for ApplyPilot.

Auto-detects providers from environment (every key that is set joins the
fallback chain, in this order):
  LLM_URL         -> Any OpenAI-compatible endpoint (LLM_API_KEY, LLM_MODEL)
  NVIDIA_API_KEY  -> NVIDIA NIM (NVIDIA_MODEL, default: nemotron-3-super-120b-a12b)
  GROQ_API_KEY    -> Groq (GROQ_MODEL, default: qwen/qwen3.8-27b)
  GEMINI_API_KEY  -> Google Gemini (GEMINI_MODEL, default: gemini-3.8-flash)
  OPENAI_API_KEY  -> OpenAI (OPENAI_MODEL, default: gpt-4o-mini)

LLM_MODEL overrides the model of the first provider in the chain. When a
provider keeps failing (quota, outage), the next one is used.
"""

import logging
import os
import re
import threading
import time

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_providers() -> list[tuple[str, str, str]]:
    """Return [(base_url, model, api_key), ...] for every configured provider.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.
    """
    env = os.environ.get
    chain: list[tuple[str, str, str]] = []
    if env("LLM_URL"):
        chain.append((env("LLM_URL").rstrip("/"), env("LLM_MODEL_LOCAL") or "local-model",
                      env("LLM_API_KEY", "")))
    if env("NVIDIA_API_KEY"):
        chain.append(("https://integrate.api.nvidia.com/v1",
                      env("NVIDIA_MODEL") or "nvidia/nemotron-3-super-120b-a12b",
                      env("NVIDIA_API_KEY")))
    if env("GROQ_API_KEY"):
        chain.append(("https://api.groq.com/openai/v1",
                      env("GROQ_MODEL") or "qwen/qwen3.8-27b", env("GROQ_API_KEY")))
    if env("GEMINI_API_KEY"):
        chain.append((_GEMINI_COMPAT_BASE, env("GEMINI_MODEL") or "gemini-3.8-flash",
                      env("GEMINI_API_KEY")))
    if env("OPENAI_API_KEY"):
        chain.append(("https://api.openai.com/v1", env("OPENAI_MODEL") or "gpt-4o-mini",
                      env("OPENAI_API_KEY")))
    if not chain:
        raise RuntimeError(
            "No LLM provider configured. Set NVIDIA_API_KEY, GROQ_API_KEY, "
            "GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL in your environment."
        )
    if env("LLM_MODEL"):
        base, _, key = chain[0]
        chain[0] = (base, env("LLM_MODEL"), key)
    return chain


def _detect_provider() -> tuple[str, str, str]:
    """First provider in the chain (kept for callers that want just one)."""
    return _detect_providers()[0]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds

# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Gemini free tier is 15 RPM = 4s minimum between requests; 10s gives headroom.
_RATE_LIMIT_BASE_WAIT = 10


_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    For Gemini keys, starts on the OpenAI-compat layer. On a 403 (which
    happens with preview/experimental models not exposed via compat), it
    automatically switches to the native generateContent API and stays there
    for the lifetime of the process.
    """

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=_TIMEOUT)
        # True once we've confirmed the native Gemini API works for this model
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the native Gemini generateContent API.

        Used automatically when the OpenAI-compat endpoint returns 403,
        which happens for preview/experimental models not exposed via compat.

        Converts OpenAI-style messages to Gemini's contents/systemInstruction
        format transparently.
        """
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                # Gemini uses "model" instead of "assistant"
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # Thinking tokens eat the max_tokens budget (and free-tier quota) and
        # the callers only parse the final answer.
        mid = self.model.lower()
        if "api.groq.com" in self.base_url and mid.startswith(("qwen/", "deepseek")):
            payload["reasoning_format"] = "hidden"
            payload["reasoning_effort"] = "none"
        elif "gpt-oss" in mid:
            payload["reasoning_effort"] = "low"
        elif self._is_gemini:
            payload["reasoning_effort"] = "low"

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        # 403 on Gemini compat = model not available on compat layer.
        # Raise a specific sentinel so chat() can switch to native API.
        if resp.status_code == 403 and self._is_gemini:
            raise _GeminiCompatForbidden(resp)

        return self._handle_compat_response(resp)

    @staticmethod
    def _handle_compat_response(resp: httpx.Response) -> str:
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"].get("content") or ""
        # Reasoning models put chain-of-thought in <think> tags; callers parse
        # the answer, so drop it.
        return re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the assistant message text."""
        # Qwen3 optimization: prepend /no_think to skip chain-of-thought
        # reasoning, saving tokens on structured extraction tasks.
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + messages[1:]

        for attempt in range(_MAX_RETRIES):
            try:
                # Route to native Gemini if we've already confirmed it's needed
                if self._use_native_gemini:
                    return self._chat_native_gemini(messages, temperature, max_tokens)

                return self._chat_compat(messages, temperature, max_tokens)

            except _GeminiCompatForbidden as exc:
                # Model not available on OpenAI-compat layer — switch to native.
                log.warning(
                    "Gemini compat endpoint returned 403 for model '%s'. "
                    "Switching to native generateContent API. "
                    "(Preview/experimental models are often compat-only on native.)",
                    self.model,
                )
                self._use_native_gemini = True
                # Retry immediately with native — don't count as a rate-limit wait
                try:
                    return self._chat_native_gemini(messages, temperature, max_tokens)
                except httpx.HTTPStatusError as native_exc:
                    raise RuntimeError(
                        f"Both Gemini endpoints failed. Compat: 403 Forbidden. "
                        f"Native: {native_exc.response.status_code} — "
                        f"{native_exc.response.text[:200]}"
                    ) from native_exc

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    # Respect Retry-After header if provided (Gemini sends this).
                    retry_after = (
                        resp.headers.get("Retry-After")
                        or resp.headers.get("X-RateLimit-Reset-Requests")
                    )
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                        if wait > 60:
                            raise  # quota window, not a blip: let the fallback chain move on
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d. "
                        "Tip: Gemini free tier = 15 RPM. Consider a paid account "
                        "or switching to a local model.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning(
                        "LLM request timed out, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class FallbackClient:
    """Tries each configured provider in order, moving on when one fails."""

    def __init__(self, clients: list[LLMClient]) -> None:
        self.clients = clients
        self._lock = threading.Lock()
        self._active = 0

    @property
    def model(self) -> str:
        return self.clients[self._active].model

    def chat(self, messages: list[dict], **kwargs) -> str:
        last_exc: Exception | None = None
        start = self._active
        for offset in range(len(self.clients)):
            idx = (start + offset) % len(self.clients)
            client = self.clients[idx]
            try:
                out = client.chat(messages, **kwargs)
                if idx != self._active:
                    with self._lock:
                        self._active = idx
                    log.warning("LLM fallback: now using %s (%s)", client.model, client.base_url)
                return out
            except Exception as exc:  # quota, auth, outage: try the next provider
                last_exc = exc
                log.warning("LLM provider %s failed: %s", client.model, str(exc)[:200])
        raise RuntimeError(f"All LLM providers failed. Last error: {last_exc}") from last_exc

    def ask(self, prompt: str, **kwargs) -> str:
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        for c in self.clients:
            c.close()


_instance: FallbackClient | None = None
_instance_lock = threading.Lock()


def get_client() -> FallbackClient:
    """Return (or create) the module-level LLM client singleton."""
    global _instance
    with _instance_lock:
        if _instance is None:
            chain = _detect_providers()
            for base_url, model, _ in chain:
                log.info("LLM provider: %s  model: %s", base_url, model)
            _instance = FallbackClient([LLMClient(*c) for c in chain])
    return _instance
