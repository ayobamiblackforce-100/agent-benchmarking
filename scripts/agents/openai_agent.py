"""Adapter for any OpenAI-compatible chat/completions API.

Covers: vLLM's built-in OpenAI server (e.g. the `multi-user-vllm` project's
:8000 endpoint), the `multi-user-vllm` router in front of multiple vLLM
backends (:9000 - see multi-user-vLLM/docs/ROUTER.md), LM Studio, Together,
Groq, the real OpenAI API, and anything else that speaks the standard
/v1/chat/completions wire format.
"""
import time

import requests


class OpenAICompatAgent:
    provider = "openai"

    def __init__(self, base_url="http://localhost:8000/v1", api_key=None, timeout=300):
        self.base_url = base_url.rstrip("/")
        # vLLM's OpenAI server ignores the key entirely but still expects the
        # header to be present; real hosted APIs (OpenAI, Together, Groq, ...)
        # require a real key here.
        self.api_key = api_key or "EMPTY"
        self.timeout = timeout

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    def generate(self, model, prompt):
        t0 = time.time()
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            timeout=self.timeout,
        )
        wall_time = time.time() - t0
        resp.raise_for_status()
        data = resp.json()

        raw_text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage", {}) or {}
        completion_tokens = usage.get("completion_tokens", 0)

        # IMPORTANT COMPARABILITY CAVEAT: OpenAI-compatible APIs don't expose a
        # separate decode-only duration the way Ollama does (total_duration vs.
        # eval_duration). This tokens_per_sec is completion_tokens / *total*
        # wall_time (queueing + prompt processing + decode + network), so it
        # will typically read lower than the Ollama adapter's tokens_per_sec
        # for an equivalent model. Don't compare the two numbers directly
        # across providers without accounting for this - see docs/AGENTS.md.
        tokens_per_sec = (
            completion_tokens / wall_time if wall_time > 0 and completion_tokens else None
        )

        raw_metrics = {"usage": usage}
        # Router pass-through (agent-benchmarking case-type 1: "test the agent
        # itself"): the multi-user-vLLM router injects an additive
        # "router_metadata" block into an otherwise-standard OpenAI response
        # (requested_model, routed_backend, routed_model_id, routing_reason,
        # classified_tier, router_wall_time_s - see multi-user-vLLM/router/app.py).
        # A plain vLLM/OpenAI server never sends this key, so this is a no-op
        # for every other provider - purely additive, doesn't change behavior
        # for non-router endpoints.
        if "router_metadata" in data:
            raw_metrics["router_metadata"] = data["router_metadata"]

        return {
            "raw_text": raw_text,
            "wall_time_s": wall_time,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": completion_tokens,
            "tokens_per_sec": tokens_per_sec,
            "provider": self.provider,
            "raw_metrics": raw_metrics,
        }

    def embed(self, text, model="text-embedding-ada-002"):
        """Only works if the server actually serves an embeddings endpoint.
        vLLM's default OpenAI chat server usually does NOT unless a dedicated
        embedding model is deployed alongside it - see docs/AGENTS.md.
        """
        resp = requests.post(
            f"{self.base_url}/embeddings",
            headers=self._headers(),
            json={"model": model, "input": text},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
