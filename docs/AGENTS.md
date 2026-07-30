# Agent Adapter Layer

`benchmark.py` (SQL generation) and `rag_pipeline.py` (embeddings) don't talk to
any model-serving backend directly. Both go through a small adapter layer in
`scripts/agents/` so the harness itself never needs a per-provider branch —
swapping Ollama for vLLM, for a router in front of multiple vLLM backends, or
for a hosted API is purely a config change.

This document is the interface contract for that layer: what each adapter
returns, which env vars control it, and a worked example per provider
(including the two router "case types" used by the `agent-benchmarking`
cross-project initiative).

## Interface contract

Every adapter class in `scripts/agents/` exposes two methods:

```python
generate(model: str, prompt: str) -> dict
```
Returns:
```python
{
  "raw_text": str,
  "wall_time_s": float,
  "prompt_tokens": int | None,
  "completion_tokens": int | None,
  "tokens_per_sec": float | None,
  "provider": str,
  "raw_metrics": dict,   # provider-specific extras, kept for debugging
}
```

```python
embed(text: str, model: str) -> list[float]
```
Only implemented where the backend actually serves embeddings — see
"Embeddings are configured separately" below.

The factory is `agents.get_agent(agent_type, base_url=None, api_key=None,
timeout=300)` in `scripts/agents/__init__.py`. `agent_type` accepts:

| `agent_type` | Adapter | Talks to |
|---|---|---|
| `ollama` | `OllamaAgent` | Ollama's native `/api/generate` + `/api/embeddings` |
| `openai`, `openai_compat`, `vllm` (aliases, same adapter) | `OpenAICompatAgent` | Any standard `/v1/chat/completions` server: vLLM's built-in OpenAI server, the `multi-user-vLLM` router, LM Studio, Together, Groq, real OpenAI API |

## `tokens_per_sec` is not comparable across providers

The Ollama adapter's `tokens_per_sec` is **decode-only** throughput
(`eval_count / eval_duration`), matching what Ollama itself reports and what
earlier phases of this project's `final_report.md` used — it excludes prompt
processing and model load time.

The OpenAI-compatible adapter has no equivalent field to read from the wire
protocol, so its `tokens_per_sec` is `completion_tokens / total_wall_time`
(queueing + prompt processing + decode + network round-trip). This will
**typically read lower** than the Ollama number for an equivalent model, even
if the underlying inference speed is the same or faster.

**Don't compare `tokens_per_sec` across the `ollama` and `openai` providers
without accounting for this.** Within the same provider, comparisons are fine.

## Embeddings are configured separately from the agent under test

`rag_pipeline.py` builds `RAG_CORPUS_PATH` using a **separately configured**
embedding agent: `EMBED_AGENT_TYPE` / `EMBED_URL` / `EMBED_MODEL` /
`EMBED_API_KEY`, independent of `AGENT_TYPE` / `AGENT_URL` (the agent under
test for SQL generation).

Why: vLLM's chat-completions server (and the router in front of it) normally
does **not** serve embeddings unless a dedicated embedding model is deployed
alongside it. In practice, even when the agent under test is vLLM, the router,
or a hosted API, you'll still point the *embedding* step at an Ollama instance
running `nomic-embed-text`. `OpenAICompatAgent.embed()` exists and will work
against any server that does implement `/v1/embeddings`, but that's the
exception, not the default setup.

**`EMBED_*` is needed at benchmark-run time too, not just at corpus-build
time.** It's easy to assume that once `rag_corpus.json` exists, the embedding
backend is no longer needed — it's still required: `retrieve()` in
`rag_pipeline.py` embeds *each test question live* to compare it against the
pre-built corpus, for every `"rag"`-strategy run. `benchmark.py` imports
`rag_pipeline`, which instantiates its embedding agent from `EMBED_*` at
**import time** — so if you only export `EMBED_*` while running
`rag_pipeline.py` to build the corpus, and forget to export the same vars
when you actually invoke `benchmark.py`, every `"rag"`-strategy test case will
silently fall back to `EMBED_URL`'s default (`http://localhost:11434`) and
fail (or silently use the wrong backend/model) partway through the run. Export
`EMBED_*` identically for both commands, always.

Also note that a *reachable* embedding endpoint isn't sufficient — it needs to
actually serve `EMBED_MODEL`. A `curl <EMBED_URL>/api/tags` returning 200 only
proves Ollama itself is up; it doesn't prove `nomic-embed-text` (or whichever
model you set) is pulled. Test with a real embed call instead, e.g.:
```bash
curl -s http://<embed-host>:11434/api/embeddings \
  -d '{"model":"nomic-embed-text","prompt":"test"}'
```

## Config reference

| Variable | Applies to | Default | Purpose |
|---|---|---|---|
| `AGENT_TYPE` | `benchmark.py` | `ollama` | `ollama` \| `openai` (aliases `openai_compat`, `vllm`) |
| `AGENT_URL` | `benchmark.py` | per-type default (`http://localhost:11434` for ollama, `http://localhost:8000/v1` for openai) — falls back to `OLLAMA_URL` if set, for backward compatibility | Base URL of the agent under test |
| `AGENT_API_KEY` | `benchmark.py` | `EMPTY` | Bearer token. vLLM ignores the value but still expects the header present; hosted APIs (OpenAI, Together, Groq) require a real key |
| `MODELS` | `benchmark.py` | provider-dependent | Comma-separated model identifiers to benchmark. Ollama: model tags (`qwen2.5-coder:32b`). OpenAI-compatible: model ids as reported by the server's `/v1/models`, or the literal string `auto` when pointed at the router (see case-type 1 below) |
| `EMBED_AGENT_TYPE` | `rag_pipeline.py` | `ollama` | Same values as `AGENT_TYPE`, configured independently |
| `EMBED_URL` | `rag_pipeline.py` | `http://localhost:11434` | Base URL of the embedding backend |
| `EMBED_MODEL` | `rag_pipeline.py` | `nomic-embed-text` | Embedding model name |
| `EMBED_API_KEY` | `rag_pipeline.py` | `EMPTY` | Same semantics as `AGENT_API_KEY` |
| `TEST_CASES_PATH` | both | `<scripts_dir>/../testcases/test_cases.json` | Gold test set |
| `RAG_CORPUS_PATH` | both | `<scripts_dir>/../testcases/rag_corpus.json` | Embedded RAG corpus (built by `rag_pipeline.py`, consumed by `benchmark.py`) |
| `RESULTS_DIR` | `benchmark.py` | `<scripts_dir>/../testcases` | Where result files land |
| `DB_DSN` / `DB_USER` / `DB_PWD` | `benchmark.py` | see `README.md` | Oracle connection |

## Worked examples

### 1. Ollama (regression path — unchanged behavior)

```bash
EMBED_AGENT_TYPE=ollama EMBED_URL=http://localhost:11434 EMBED_MODEL=nomic-embed-text \
  python3 scripts/rag_pipeline.py

AGENT_TYPE=ollama AGENT_URL=http://localhost:11434 \
MODELS=qwen2.5-coder:32b,qwen2.5-coder:32b-instruct-q8_0 \
  python3 scripts/benchmark.py
```

### 2. vLLM, single model (also covers any plain OpenAI-compatible server)

```bash
AGENT_TYPE=openai AGENT_URL=http://<vllm-host>:8000/v1 \
MODELS=Qwen/Qwen2.5-32B-Instruct \
  python3 scripts/benchmark.py
```
Same adapter code path as the router below — a plain vLLM server just never
populates `router_metadata` (see next section), so it's a no-op field.

### 3. Router, case-type 1 — test the router's own internal routing (`MODELS=auto`)

The `multi-user-vLLM` router speaks the exact same `/v1/chat/completions` wire
format as plain vLLM, so **no new `AGENT_TYPE` is needed** — `openai` (or its
`vllm` alias) already works against it. Passing `model="auto"` tells the
router to classify the prompt's complexity itself and pick a backend tier.

```bash
AGENT_TYPE=openai AGENT_URL=http://<router-host>:9000/v1 \
MODELS=auto \
  python3 scripts/benchmark.py
```

`openai_agent.py` opportunistically captures the router's additive
`router_metadata` block (`requested_model`, `routed_backend`,
`routed_model_id`, `routing_reason`, `classified_tier`,
`router_wall_time_s`) into each result's `raw_metrics_json` whenever the
response includes one. Validated live 2026-07-29 against
`root@162.243.121.249:9000`: 140/140 runs completed, 134 `exact_match`,
`classified_tier` correctly spans all 3 tiers across the run.

### 4. Router, case-type 2 — test each backend model individually (bypass routing)

Pass one or more of the router's *configured backend model ids* directly
(as reported by the router's `/v1/models`) instead of `auto`. The router
detects this isn't `auto`, skips classification entirely, and routes straight
to that backend.

```bash
AGENT_TYPE=openai AGENT_URL=http://<router-host>:9000/v1 \
MODELS=Qwen/Qwen2.5-7B-Instruct-AWQ,Qwen/Qwen2.5-14B-Instruct \
  python3 scripts/benchmark.py
```

Each result's `router_metadata.classified_tier` will be `null` and
`routing_reason` will read `"pinned to model_id '<id>'"`, proving the
classifier was bypassed rather than coincidentally agreeing with the pin.
Validated live 2026-07-29: 280/280 runs completed (both pinned models x 2
context strategies), 258 `exact_match`, `classified_tier=null` and the
`"pinned to model_id ..."` reason on every one of the 280 rows, with zero
mismatches between the requested pinned model and `routed_model_id`.

## Adding a new provider

1. Add a new adapter class under `scripts/agents/` implementing `generate()`
   and (optionally) `embed()` with the return shapes above.
2. Register it in `get_agent()` in `scripts/agents/__init__.py`.
3. If it needs a new `agent_type` string, document it in the table above.
   If it's just another server that speaks the standard OpenAI wire format
   (most hosted APIs do), you don't need a new adapter at all — just point
   `AGENT_TYPE=openai` at it with the right `AGENT_URL`/`AGENT_API_KEY`.
