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
| `--no-resource-monitor` | *(on by default)* | Skip CPU/mem/GPU utilization sampling |

Example (what produced the results below — DB and agent co-located on the
GPU host, matching how the real deployment would be run):

```
./run_concurrency_benchmark.sh \
  --agent-type ollama --agent-url http://localhost:11434 --model qwen2.5-coder:32b \
  --db-dsn localhost:1521/FREEPDB1 \
  --levels 1,2,4,8,16,32 --rounds 10 --context-strategy static
```

**Output** (under `testcases/`): `concurrency_results.{json,csv}` (per
request), `concurrency_summary.{json,csv}` (per level: throughput, latency
percentiles, error rate, correctness), and `concurrency_report.md` (findings +
recommendations, generated from the measured numbers, not a static template).

## Resource utilization (CPU/mem/GPU)

As of this update, the harness samples CPU, memory, and (when `nvidia-smi`
is present) GPU utilization every 0.5s during each concurrency level, via
`psutil` + `nvidia-smi`. This closes a real gap: p95 latency alone can show
"agent generation is the bottleneck," but can't say *why* — whether the
GPU is genuinely maxed out (a hardware ceiling — the fix is more GPU
capacity) or sitting idle while the serving stack just isn't parallelizing
(a config ceiling — e.g. Ollama's default single-worker behavior — fixable
without new hardware). Utilization is what tells those two apart.

**Co-location requirement:** this only means something if the harness runs
on the same host as the agent under test. If `--agent-url` points at a
remote host, local CPU/GPU stats would describe the wrong machine, so the
harness detects that (`--agent-url` is not localhost) and omits the
utilization table entirely rather than reporting numbers that look valid
but are meaningless.

Validated with a small smoke run (levels 1,4; 2 rounds) on the H100 box
before trusting it for a full sweep: GPU utilization climbed from ~20% avg
at concurrency=1 to ~91% avg at concurrency=4, with CPU/mem staying low
throughout (confirms this is GPU-bound work, not a host resource issue) —
sampling behaved as expected. A bug caught in the process: the DB
connection pool's `POOL_MAX` sizing constant was accidentally dropped
during the file rewrite that added resource monitoring, causing an
immediate `NameError` on every run; fixed and re-verified.

## Result analysis (real run, H100 GPU target)

Agent: `qwen2.5-coder:32b` (Q4_K_M) via Ollama, single instance, on the H100
80GB target (`162.243.213.185`) · DB: seeded Oracle 23ai, co-located on the
same host · 630 requests total across levels 1/2/4/8/16/32, 10 rounds/level.

| Level | Requests | Avg Score | Throughput (req/s) | Total p50 | Total p95 | Agent p95 | DB p95 |
|---|---|---|---|---|---|---|---|
| 1 | 10 | 0.40 | 0.787 | 1.24s | 2.05s | 1.85s | 0.69s |
| 2 | 20 | 0.35 | 1.003 | 1.46s | 3.28s | 3.00s | 0.88s |
| 4 | 40 | 0.48 | 1.332 | 2.63s | 4.89s | 4.39s | 1.17s |
| 8 | 80 | 0.39 | 1.117 | 6.90s | 9.41s | 9.29s | 0.94s |
| 16 | 160 | 0.36 | 1.128 | 13.55s | 17.71s | 17.00s | 1.07s |
| 32 | 320 | 0.44 | 1.174 | 26.95s | 32.31s | 32.14s | 1.09s |

**Bottleneck: the model server, not the database — same finding as the
earlier small-model run, but now on real GPU hardware where it's a cleaner
signal.** Real parallelism shows up briefly at low concurrency (throughput
climbs 0.79→1.33 req/s from level 1→4, some genuine benefit from Ollama's
default request batching), but it saturates hard by level 8 and stays
essentially flat (1.12–1.17 req/s) all the way to level 32 — an 8x further
increase in concurrency beyond that point buys almost nothing. Overall
scaling efficiency from level 1→32 is only ~5%. Meanwhile p95 agent-generation
time grows almost linearly with concurrency (1.85s→32.1s, 17.4x) while p95 DB
time stays flat under 1.1s throughout (1.6x growth) — DB was never a factor
here. Correctness held steady across the sweep (0.40 at level 1 vs 0.44 at
level 32), so this is a pure latency/throughput ceiling, not a quality
degradation.

**Recommendation:** this is a single Ollama instance serving one model on one
GPU — the plateau at ~1.1–1.2 req/s past level 8 is the ceiling of one
generation running at a time (with a small amount of request batching, not
true parallelism). To raise it: deploy multiple backend replicas behind
`multi-user-vLLM`'s router so concurrent requests actually run across
independent GPU workers, or switch to vLLM and tune continuous-batching
settings (`max_num_seqs`, `max_num_batched_tokens`) so the GPU processes many
requests together rather than one at a time — an H100 with 80GB VRAM serving
a 20GB model has plenty of headroom for that.

### Earlier (smaller, resource-constrained) reference run

For comparison, an earlier pass on a laptop with only ~1.5GB free RAM running
`qwen2.5-coder:1.5b` (150 requests, levels 1/2/4/8, 10 rounds/level) showed
the same *shape* of bottleneck but far worse absolute numbers — throughput
0.197→0.212 req/s (barely 13% scaling efficiency even at 8x lower peak
concurrency) and correctness actively degrading under load (0.40→0.26),
consistent with a genuinely resource-starved host rather than just an
unparallelized model server. Useful as confirmation the harness generalizes
across very different hardware, but not representative of the production
target.
