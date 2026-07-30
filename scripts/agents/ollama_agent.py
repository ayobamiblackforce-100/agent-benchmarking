"""Adapter for Ollama's native API (/api/generate, /api/embeddings).

This is the original call_model()/embed() logic from benchmark.py and
rag_pipeline.py, unchanged in behavior, just moved behind the common
agent interface (see agents/__init__.py) so it can be swapped for other
backends without touching the harness code.
"""
import time

import requests


class OllamaAgent:
    provider = "ollama"

    def __init__(self, base_url="http://localhost:11434", timeout=300):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def generate(self, model, prompt):
        t0 = time.time()
        resp = requests.post(
            f"{self.base_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0}},
            timeout=self.timeout,
        )
        wall_time = time.time() - t0
        resp.raise_for_status()
        data = resp.json()

        eval_count = data.get("eval_count", 0)
        eval_duration_s = data.get("eval_duration", 0) / 1e9
        # decode-only throughput (excludes prompt processing + model load), matches
        # the metric historically reported in final_report.md for the Ollama runs
        tokens_per_sec = eval_count / eval_duration_s if eval_duration_s > 0 else None

        return {
            "raw_text": data.get("response", ""),
            "wall_time_s": wall_time,
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": eval_count,
            "tokens_per_sec": tokens_per_sec,
            "provider": self.provider,
            "raw_metrics": {
                "total_duration_s": data.get("total_duration", 0) / 1e9,
                "load_duration_s": data.get("load_duration", 0) / 1e9,
                "prompt_eval_duration_s": data.get("prompt_eval_duration", 0) / 1e9,
                "eval_duration_s": eval_duration_s,
            },
        }

    def embed(self, text, model="nomic-embed-text"):
        resp = requests.post(
            f"{self.base_url}/api/embeddings",
            json={"model": model, "prompt": text},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]
