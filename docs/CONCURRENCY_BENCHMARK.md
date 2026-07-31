# Concurrency Benchmark — Knowledge Article

Companion to the main NL→SQL correctness/latency benchmark (`run_benchmark.sh`).
That one sweeps *models/context strategies* one request at a time. This one
holds the model and context strategy **fixed** and sweeps **concurrency**
instead — firing an increasing number of simultaneous requests at the agent
under test to answer: does correctness hold up, and how does response time
degrade, as concurrent load grows? It also auto-generates a bottleneck
analysis from the measured numbers.

## What the percentiles mean

`p50` / `p95` / `p99` are **latency, in seconds** — not throughput, and not a
count. They're computed separately for three timings per request:

| Metric | What it measures |
|---|---|
| `total_time_s` | Full request: prompt build → agent call → DB exec → scoring |
| `agent_wall_time_s` | Just the model-generation call |
| `db_exec_time_s` | Just the SQL execution against Oracle |

For a given concurrency level, all requests' values are sorted and:
- **p50** (median) — half the requests were faster than this.
- **p95** — 95% of requests were faster than this; the slowest 5% exceeded it.
- **p99** — same idea, slowest 1%.

**Throughput (req/s) is a separate, single number per level** — not a
percentile — computed as `n_requests / level_wall_time_s` (requests completed
divided by how long the whole level actually took, wall-clock).

**Sample-size caveat:** percentiles are only as trustworthy as the sample
behind them. At low request counts, "p99" is effectively just the slowest
request in the batch, not a stable tail estimate. As a rule of thumb, you want
comfortably more than 20 requests above the percentile threshold itself — so
p95 wants dozens of requests per level, and p99 really wants hundreds.

## Script usage

```
./run_concurrency_benchmark.sh --agent-url URL --model MODEL_ID [options]
```

Key flags (full list: `--help`):

| Flag | Default | Purpose |
|---|---|---|
| `--agent-url URL` | *(required)* | Base URL of the agent under test |
| `--agent-type TYPE` | `openai` | `openai` (vLLM/router/hosted APIs) or `ollama` |
| `--model MODEL` | auto-detected | Model id — held fixed; concurrency is the only variable |
| `--levels LIST` | `1,2,4,8,16,32` | Comma-separated concurrency levels to sweep |
| `--rounds N` | `3` | Requests per worker per level — total at level `L` = `L * N` |
| `--context-strategy S` | `static` | `static` or `rag`, held fixed across the sweep |
| `--db-dsn` / `--db-user` / `--db-pwd` | local defaults | Oracle connection (can point at a remote DB) |

Example (what produced the results below):

```
./run_concurrency_benchmark.sh \
  --agent-type ollama --agent-url http://localhost:11434 --model qwen2.5-coder:1.5b \
  --db-dsn 162.243.213.185:1521/FREEPDB1 \
  --levels 1,2,4,8 --rounds 10 --context-strategy static
```

**Output** (under `testcases/`): `concurrency_results.{json,csv}` (per
request), `concurrency_summary.{json,csv}` (per level: throughput, latency
percentiles, error rate, correctness), and `concurrency_report.md` (findings +
recommendations, generated from the measured numbers, not a static template).

## Result analysis (real run)

Agent: `qwen2.5-coder:1.5b` via Ollama (single local instance) · DB: seeded
Oracle 23ai · 150 requests total across levels 1/2/4/8, 10 rounds/level.

| Level | Requests | Avg Score | Throughput (req/s) | Total p50 | Total p95 | Agent p95 | DB p95 |
|---|---|---|---|---|---|---|---|
| 1 | 10 | 0.40 | 0.197 | 5.18s | 7.51s | 7.38s | 1.11s |
| 2 | 20 | 0.35 | 0.159 | 12.78s | 20.14s | 20.01s | 2.99s |
| 4 | 40 | 0.40 | 0.232 | 15.56s | 25.23s | 23.18s | 3.58s |
| 8 | 80 | 0.26 | 0.212 | 36.01s | 57.57s | 55.51s | 2.50s |

**Bottleneck: the model server, not the database.** Going from 1→8 concurrent
requests is an 8x increase, but throughput only moved 0.197→0.212 req/s
(~13% scaling efficiency) — the system is serializing work, not parallelizing
it. p95 agent-generation time grew 7.5x (7.4s→55.5s) while p95 DB time grew
only 2.25x and stayed under 4s throughout. This is the expected signature of
a single Ollama instance processing one generation at a time: correctness
also dropped under load (0.40→0.26 avg score), consistent with a
resource-starved single worker rather than a healthy system.

**Recommendation:** this result reflects one small model on one
resource-constrained instance — it's a real demonstration of the harness
working correctly (it flagged a genuine, expected bottleneck), not a verdict
on the production stack. For a representative read on production hardware,
re-run against the multi-user-vLLM router or a properly-resourced Ollama/vLLM
instance, and use a higher `--rounds` (≥20) at each level so the p95/p99
figures are statistically meaningful rather than tail-of-10-samples noise.
