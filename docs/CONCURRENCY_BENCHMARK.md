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

## Result analysis: Ollama vs. vLLM (real H100 GPU target)

Same model family (`Qwen2.5-Coder-32B-Instruct`), same DB (seeded Oracle
23ai, co-located on the same host), same H100 80GB target
(`162.243.213.185`), same sweep (levels 1/2/4/8/16/32, 10 rounds/level, 630
requests) - agent serving is the only variable. Ollama used the `qwen2.5-coder:32b`
Q4_K_M GGUF quant (the model as normally pulled via `ollama pull`); vLLM used
the full-precision `Qwen/Qwen2.5-Coder-32B-Instruct` bf16 weights (this
harness's `multi-user-vLLM` companion project auto-resolves serving config
from a GPU-tier/concurrency lookup table - see that repo's README). Both
runs include CPU/mem/GPU utilization sampling.

### Ollama (single instance, one generation at a time)

| Level | Throughput (req/s) | Total p95 | Agent p95 | DB p95 | GPU util avg/max |
|---|---|---|---|---|---|
| 1 | 0.56 | 4.67s | 4.53s | 0.68s | 44.8/92.0% |
| 2 | 1.01 | 3.28s | 2.99s | 0.87s | 82.4/92.0% |
| 4 | 1.33 | 4.87s | 4.38s | 1.18s | 81.9/92.0% |
| 8 | 1.12 | 9.48s | 9.34s | 0.94s | 84.4/92.0% |
| 16 | 1.13 | 17.78s | 17.03s | 1.12s | 86.6/92.0% |
| 32 | 1.13 | 35.09s | 34.74s | 1.12s | 82.3/93.0% |

Throughput plateaus at ~1.1-1.3 req/s from level 2 onward (32x concurrency
buys ~2x throughput - ~6% scaling efficiency). Agent p95 grows almost
linearly with concurrency (4.53s -> 34.74s, 7.67x); DB p95 stays flat
(0.68s -> 1.12s, 1.64x) - never a factor. **GPU utilization is already
44.8-86.6% at every level, including concurrency=1** - because Ollama serves
one generation at a time, a single in-flight request alone pins the GPU
during its own decode step; queue depth doesn't change per-request GPU
demand, it just makes more requests wait in line. Bottleneck: agent/model
serving, not hardware capacity in the "need more GPUs" sense - the ceiling is
architectural (no request batching), not raw compute.

### vLLM (continuous batching, `max_num_seqs=32`)

| Level | Throughput (req/s) | Total p95 | Agent p95 | DB p95 | GPU util avg/max |
|---|---|---|---|---|---|
| 1 | 0.68 | 2.61s | 2.36s | 0.72s | 83.2/100.0% |
| 2 | 0.99 | 6.45s | 2.98s | 1.60s | 82.5/100.0% |
| 4 | 2.78 | 3.75s | 2.67s | 1.17s | 92.2/100.0% |
| 8 | 4.12 | 5.12s | 3.06s | 1.10s | 94.4/100.0% |
| 16 | 6.45 | 8.03s | 3.29s | 3.71s | 86.4/100.0% |
| 32 | 9.74 | 6.79s | 3.17s | 4.26s | 66.4/100.0% |

Throughput keeps climbing all the way to level 32 (0.68 -> 9.74 req/s, ~45%
scaling efficiency - far from perfectly linear, but a completely different
shape from Ollama's hard plateau). **Agent p95 stays nearly flat across the
whole sweep** (2.36s -> 3.17s, 1.35x growth) - continuous batching absorbs
concurrent requests instead of queueing them one at a time. **DB p95 instead
becomes the growing term** (0.72s -> 4.26s, 5.92x growth) and overtakes agent
p95 as the larger latency component by level 16. Bottleneck: shifts to the
Oracle side - once the model server stops being the constraint, the DB
connection pool / exec path becomes the next one.

### Head-to-head at concurrency=32

| Metric | Ollama | vLLM | vLLM vs. Ollama |
|---|---|---|---|
| Throughput | 1.13 req/s | 9.74 req/s | **8.6x higher** |
| Total p95 | 35.09s | 6.79s | **5.2x lower** |
| Agent p95 | 34.74s | 3.17s | **11x lower** |
| Agent p95 growth (1->32) | 7.67x | 1.35x | far flatter |
| DB p95 | 1.12s | 4.26s | 3.8x higher (new constraint) |

**Bottom line:** the level-8+ plateau seen in the Ollama run is a genuine
serving-architecture ceiling, not a GPU hardware ceiling - the same H100,
same model family, serving via vLLM's continuous batching instead of
Ollama's one-at-a-time generation, sustains ~8.6x more throughput at the same
concurrency with agent latency barely growing under load. The GPU-utilization
sampling is what makes this legible: Ollama's GPU was *already* 80-90%
utilized at every level (each single in-flight generation is compute-heavy),
which on its own could be misread as "GPU-bound, buy more hardware" - but
vLLM proves the same card had far more throughput available once requests
could be batched rather than serialized. Once agent-side latency stopped
being the constraint, database exec time became the next bottleneck (DB p95
overtakes agent p95 by level 16) - worth a follow-up pass on Oracle
connection-pool sizing / indexing if pushing vLLM concurrency further.

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
