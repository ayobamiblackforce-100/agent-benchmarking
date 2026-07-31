# agent-benchmarking

One-touch NL→SQL benchmark runner. Point `run_benchmark.sh` at any agent —
Ollama, vLLM, a router in front of multiple vLLM backends, or any
OpenAI-compatible API — and it validates connectivity, builds the RAG corpus
if needed, runs the full 70-question x 2-strategy Oracle NL→SQL benchmark
against it, and reports scored results.

This is a self-contained, vendored-in copy of the harness from
[`ollama-rag`](../ollama-rag) (`scripts/benchmark.py`, `scripts/rag_pipeline.py`,
`scripts/agents/`) plus the test set and a pre-built RAG corpus, wrapped in a
single CLI script so you don't need to juggle env vars by hand.

## Quickstart

```bash
git clone https://github.com/ayobamiblackforce-100/agent-benchmarking.git
cd agent-benchmarking
./run_benchmark.sh --agent-url http://<your-agent-host>:<port> --models <model_id>
```

That's it — the script will:
1. Create a local `.venv` and install `oracledb` + `requests` if not already present.
2. Check the Oracle DB, the agent under test, and the embedding backend are all
   reachable (with a real embed call, not just a ping).
3. Build `testcases/rag_corpus.json` if it doesn't already exist (skipped here —
   one's already checked in).
4. Run all 70 test cases x 2 context strategies (`static`, `rag`) against your
   agent, scoring each generated SQL query against Oracle.
5. Print a status/score summary and write full results to
   `testcases/benchmark_results.{json,csv}`.

## Usage

```
./run_benchmark.sh --agent-url URL [options]
```

Run `./run_benchmark.sh --help` for the full option list. Key ones:

| Flag | Default | Purpose |
|---|---|---|
| `--agent-url URL` | *(required)* | Base URL of the agent under test |
| `--agent-type TYPE` | `openai` | `openai` (vLLM/router/hosted APIs) or `ollama` |
| `--models LIST` | auto-detected | Comma-separated model id(s), or `auto` for a router's internal routing |
| `--db-dsn DSN` | `localhost:1521/FREEPDB1` | Oracle connection string |
| `--db-user` / `--db-pwd` | `bench` / `BenchmarkPwd123` | DB credentials |
| `--embed-url URL` | `http://localhost:11434` | Embedding backend — **independent of `--agent-url`**, see below |
| `--embed-model MODEL` | `nomic-embed-text` | Embedding model name |
| `--skip-corpus-build` | off | Fail instead of building the corpus if it's missing |
| `--force-rebuild-corpus` | off | Rebuild the corpus even if it already exists |
| `--skip-checks` | off | Skip all pre-flight reachability checks |

Full contract (return shapes, env var reference, `tokens_per_sec` caveats):
see `docs/AGENTS.md`.

### Important: the embedding backend is separate from the agent under test

`--embed-url`/`--embed-model` configure the backend used to build/query the
RAG corpus, **independent of `--agent-url`** (the agent whose SQL generation
you're actually benchmarking). Most agents under test — vLLM, a router, a
hosted API — don't serve embeddings, so you'll usually still point
`--embed-url` at an Ollama instance with `nomic-embed-text` pulled, even when
benchmarking something else entirely. And this matters for **every** run, not
just the first one that builds the corpus — every `"rag"`-strategy test
question gets embedded live at run time to match against the corpus.

## Working samples

### 1. Router (`multi-user-vLLM`), testing its own internal routing

```bash
./run_benchmark.sh \
  --agent-url http://162.243.121.249:9000 \
  --models auto \
  --db-dsn 162.243.121.249:1521/FREEPDB1 \
  --embed-url http://162.243.121.249:11434
```
```
=== agent-benchmarking: one-touch run ===
  agent_type=openai  agent_url=http://162.243.121.249:9000/v1  models=auto
  db_dsn=162.243.121.249:1521/FREEPDB1

--- Checking DB reachability (162.243.121.249:1521/FREEPDB1) ---
OK: DB reachable.

--- Checking agent reachability (http://162.243.121.249:9000/v1) ---
OK: agent reachable. Models reported:
  - auto
  - Qwen/Qwen2.5-7B-Instruct-AWQ
  - Qwen/Qwen2.5-14B-Instruct

--- Checking embedding backend (ollama @ http://162.243.121.249:11434, model=nomic-embed-text) ---
OK: embedding backend reachable and nomic-embed-text responds.

--- RAG corpus already exists at .../testcases/rag_corpus.json, skipping build ---

--- Running benchmark.py ---
Loaded 70 test cases (excluded ['hr_3_08', 'sales_3_06']), corpus size 82
...
Done. 140 runs. Saved to .../testcases/benchmark_results.json and .csv

--- Summary ---
Total runs in results file: 140
Status breakdown: {'exact_match': 134, 'error': 6}
Average score: 0.957
```

### 2. Router, pinning a specific backend model (bypass routing)

```bash
./run_benchmark.sh \
  --agent-url http://162.243.121.249:9000 \
  --models Qwen/Qwen2.5-7B-Instruct-AWQ \
  --db-dsn 162.243.121.249:1521/FREEPDB1 \
  --embed-url http://162.243.121.249:11434
```

### 3. Plain vLLM / any OpenAI-compatible server, no model specified

`--models` is optional for `--agent-type openai` — the script queries
`/v1/models` and picks the first non-`auto` id it finds:

```bash
./run_benchmark.sh --agent-url http://localhost:8000 --db-dsn <dsn>
```
```
--- Checking agent reachability (http://localhost:8000/v1) ---
OK: agent reachable. Models reported:
  - Qwen/Qwen2.5-32B-Instruct
Note: --models not given, auto-selected: Qwen/Qwen2.5-32B-Instruct
```

### 4. Ollama

```bash
./run_benchmark.sh \
  --agent-url http://localhost:11434 --agent-type ollama \
  --models qwen2.5-coder:32b \
  --db-dsn <dsn>
```
(`--embed-url` defaults to `http://localhost:11434` too — fine here since it's
the same Ollama instance already serving the agent under test, *as long as*
`nomic-embed-text` is also pulled on it — see failure mode below.)

## Possible errors / failure logs, by cause

### DB unreachable
```
--- Checking DB reachability (localhost:1521/FREEPDB1) ---
FAILED: DPY-6005: cannot connect to database (CONNECTION_ID=...).
[Errno 61] Connection refused
```
**Fix:** wrong `--db-dsn`, DB not running, or DB only reachable from a
different host (e.g. it's on a remote server — pass the server's IP:port, not
`localhost`).

### Agent under test unreachable
```
--- Checking agent reachability (http://localhost:12225/v1) ---
FAILED: could not reach http://localhost:12225/v1/models — is the agent running?
```
**Fix:** confirm the agent process is actually up and listening on that
port/host, and that `--agent-type` matches what it speaks (`openai` for
anything with a `/v1/chat/completions` endpoint — vLLM, a router, LM Studio,
hosted APIs; `ollama` for Ollama's native API).

### Embedding backend unreachable or missing the model
This is the most common failure mode and easy to miss, because it can look
like the corpus step succeeded and only fails later, mid-run:
```
--- Checking embedding backend (ollama @ http://localhost:11434, model=nomic-embed-text) ---
FAILED: embed call to http://localhost:11434 (model=nomic-embed-text) failed:
  404 Client Error: Not Found for url: http://localhost:11434/api/embeddings
  This is EMBED_URL/EMBED_MODEL, independent of --agent-url - needed for every
  'rag'-strategy run, not just building the corpus. If Ollama is up but missing
  the model: ollama pull nomic-embed-text — or point --embed-url at a host that
  already has it (e.g. wherever the existing rag_corpus.json was built).
```
**Fix:** `ollama pull nomic-embed-text` on whichever host `--embed-url` points
at, or point `--embed-url` at a host that already has it. Note this check does
a *real* embed call — an Ollama instance can be up and pass a plain
reachability ping while still missing the specific model, which is exactly
what happened during initial testing of this script (Ollama running locally
with `qwen2.5-coder:14b` only, no `nomic-embed-text`).

### `--models` required but not given
```
ERROR: --models is required for --agent-type ollama (no reliable default).
```
or (with `--skip-checks`):
```
ERROR: --models is required when --skip-checks is set (no auto-detect possible).
```
**Fix:** pass `--models`. Auto-detection only works for `--agent-type openai`
with checks enabled (it reads `/v1/models`).

### `EMBED_*` not applied to the actual benchmark run (fixed, historical)
Earlier versions of this script exported `EMBED_*` only while building the
corpus, not while running `benchmark.py` itself — since `benchmark.py`
imports `rag_pipeline`, which instantiates its embedding agent from `EMBED_*`
at import time, and every `"rag"`-strategy question is embedded live. This
silently fell back to `localhost:11434`'s default and failed partway through
a run with the same `404 ... /api/embeddings` error above, even when
`--embed-url` was correctly passed to a *different* pre-flight-checked host.
Fixed — `EMBED_*` is now passed to both steps identically. Documented here as
a caution if you're modifying the script further.

### Per-row failures inside the results (not script failures)
Not every `status: "error"` row in `benchmark_results.json` means something
is broken — it usually means the model under test generated SQL that Oracle
rejected. This is a legitimate benchmark result, not a harness bug:
```json
{
  "test_case_id": "sales_2_12",
  "model": "auto",
  "context_strategy": "rag",
  "generated_sql": "SELECT p.PRODUCT_NAME, COUNT(DISTINCT oi.CUSTOMER_ID) ...",
  "status": "error",
  "score": 0.0,
  "sql_error": "ORA-00904: \"OI\".\"CUSTOMER_ID\": invalid identifier\n..."
}
```
Here the model referenced a column (`CUSTOMER_ID`) on the wrong table alias —
a real reasoning mistake by the model, not a harness/router bug. Compare
against a passing row:
```json
{
  "test_case_id": "sales_1_01",
  "status": "exact_match",
  "score": 1.0,
  "sql_error": null
}
```
Check `status` breakdown in the run summary (`exact_match` / `error` /
`mismatch` / `gold_error` / `model_call_error`) to distinguish model-quality
issues (`error`, `mismatch`) from actual call failures (`model_call_error` —
the agent itself couldn't be reached or errored for that specific request;
these get retried on the next run since only *successful* runs are recorded
in the resume-skip set).

## Results

`testcases/benchmark_results.json` (full detail, including `raw_metrics_json`
with provider-specific extras like a router's `router_metadata`) and
`testcases/benchmark_results.csv` (flat, for quick spreadsheet inspection).
Runs are resumable — re-running the same command skips `(test_case_id, model,
context_strategy)` combos already completed, so an interrupted run can just
be re-launched.

## Concurrency Benchmark

Companion to the main benchmark above. That one sweeps models/strategies one
request at a time; `run_concurrency_benchmark.sh` holds the model and context
strategy **fixed** and instead sweeps **concurrency** - firing an increasing
number of simultaneous NL->SQL requests at the agent under test (and Oracle)
to answer: does correctness hold up, and how does response time degrade, as
concurrent load grows? It also writes a bottleneck-analysis report computed
from the measured numbers - not a static template.

```
./run_concurrency_benchmark.sh --agent-url URL --model MODEL_ID [options]
```

Run `./run_concurrency_benchmark.sh --help` for the full option list. Key ones:

| Flag | Default | Purpose |
|---|---|---|
| `--agent-url URL` | *(required)* | Base URL of the agent under test |
| `--agent-type TYPE` | `openai` | `openai` (vLLM/router/hosted APIs) or `ollama` |
| `--model MODEL` | auto-detected | Single model id/tag - concurrency is the only variable under test, so the model is held fixed |
| `--levels LIST` | `1,2,4,8,16,32` | Comma-separated concurrency levels to sweep |
| `--rounds N` | `3` | Requests per worker at each level - total requests at level L = `L * N` |
| `--context-strategy S` | `static` | `static` or `rag`, held fixed across the whole sweep |
| `--db-dsn` / `--db-user` / `--db-pwd` | same as above | Oracle connection |
| `--no-resource-monitor` | *(monitoring on)* | Skip CPU/mem/GPU utilization sampling |

At each level, `L` worker threads run in parallel and together fire `L * N`
total requests (mirrors how load tools like k6/Locust scale virtual users).
Each request times the agent call and the DB execution **separately**, so the
report can tell agent-bound slowdowns apart from DB-bound ones, and each
request is scored against gold SQL with the same scoring logic as the main
benchmark - correctness isn't assumed to hold under load, it's checked.

### Output

- `testcases/concurrency_results.{json,csv}` — one row per request
- `testcases/concurrency_summary.{json,csv}` — one row per concurrency level:
  throughput (req/s), latency percentiles (p50/p95/p99, total + agent-only +
  DB-only), error rate, correctness (avg score, exact-match rate)
- Resource utilization (CPU/mem/GPU, via `psutil` + `nvidia-smi`) is sampled
  every 0.5s per level and included in the summary and report **only when
  the harness runs on the same host as the agent under test** - if
  `--agent-url` points elsewhere, sampling this host would describe the
  wrong machine, so it's auto-skipped rather than reported. This is what
  distinguishes "GPU genuinely saturated, buy more capacity" from "GPU
  idle, it's a serving-config ceiling (e.g. Ollama's default parallelism)"
  - the same p95 bottleneck shape can mean either, and utilization is what
  tells them apart.
- `testcases/concurrency_report.md` — findings + recommendations, generated
  from the measured summary data: throughput scaling efficiency vs. ideal
  linear scaling, which stage (agent vs. DB) dominates and grows fastest with
  concurrency, whether errors rise under load, and whether correctness holds
  up. Recommendations are conditioned on what was actually observed - e.g. an
  agent-bound bottleneck points at model-serving concurrency (multiple
  backend replicas behind [`multi-user-vLLM`](https://github.com/ayobamiblackforce-100/multi-user-vllm)'s
  router, or vLLM continuous-batching settings), while a DB-bound one points
  at connection pool sizing or missing indexes.

**Labeled comparison runs** live under `results/<label>/` (e.g. `results/ollama_full/`, `results/vllm_full/`) when you want to keep multiple full sweeps around side by side - pass `--results-dir results/<label>`. See `docs/CONCURRENCY_BENCHMARK.md` for a real Ollama-vs-vLLM comparison on the same H100 target: same model family, same DB, same sweep - vLLM's continuous batching sustained ~8.6x more throughput at concurrency=32 with agent p95 barely growing under load, while Ollama's one-generation-at-a-time serving hit a hard plateau by level 8 despite the GPU already running hot at every level.

## What's in this directory

```
run_benchmark.sh              one-touch CLI: correctness/latency sweep across models x strategies
run_concurrency_benchmark.sh   one-touch CLI: concurrency sweep at a fixed model + strategy
scripts/
  benchmark.py                 harness — runs test cases, scores generated SQL
  concurrency_benchmark.py      harness — concurrency sweep + bottleneck analysis
  rag_pipeline.py                builds/queries the RAG corpus
  agents/                         pluggable agent adapters (ollama, openai-compatible)
testcases/
  test_cases.json                  70-question gold test set (sales + HR schemas)
  rag_corpus.json                   pre-built RAG corpus (table + example embeddings)
  benchmark_results.*                written by each run_benchmark.sh run
  concurrency_results.*              written by each run_concurrency_benchmark.sh run (per-request)
  concurrency_summary.*              written by each run_concurrency_benchmark.sh run (per-level)
  concurrency_report.md              bottleneck findings + recommendations
docs/AGENTS.md                  full interface/config contract for scripts/agents/
SDD-PLAN.md                     design doc for the pluggable-agent + router initiative
```

Source project: [`ollama-rag`](https://github.com/ayobamiblackforce-100/ollama-oracle-rag)
(branch `benchmark-db-setup`) — DB setup, original harness, and full project
history. Related: [`multi-user-vLLM`](https://github.com/ayobamiblackforce-100/multi-user-vllm)
(the router used in the router examples above).
