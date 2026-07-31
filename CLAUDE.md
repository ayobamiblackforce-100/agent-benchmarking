# CLAUDE.md - agent-benchmarking Operating Log

## Standing Config (always use)
- Access to local terminal = bash-terminal MCP tools (run_command/write_file/read_file) — ONLY these reach the real filesystem; Claude's native sandbox tools (str_replace, create_file) do not.
- Project directory (local) = /Users/ayobamishittu/Downloads/projects/agent-benchmarking
- Related projects: ../ollama-rag (Oracle NL→SQL benchmark harness, DB schema/data), ../multi-user-vLLM (router)
- This repo is the coordination home for cross-project benchmarking work.

## Log

### Concurrency benchmark test case added
- Tool used: bash-terminal MCP (write_file/run_command) against the real local machine; git for version control.
- Directory touched: agent-benchmarking (scripts/concurrency_benchmark.py, run_concurrency_benchmark.sh, README.md, docs/CONCURRENCY_BENCHMARK.md, .gitignore).
- Added `scripts/concurrency_benchmark.py` + `run_concurrency_benchmark.sh`: a new test case, companion to `run_benchmark.sh`. Holds model + context strategy fixed and sweeps concurrency (ThreadPoolExecutor, configurable levels/rounds), timing agent-call and DB-exec separately via a pooled oracledb connection sized to the peak concurrency level. Scores correctness with the same logic as `benchmark.py`. Outputs per-request + per-level result files and an auto-generated `concurrency_report.md` (findings/recommendations computed from the measured numbers, not boilerplate).
- Added `.env` to `.gitignore` (was untracked but not ignored — contains Docker Hub creds; caught before first commit touched it).
- Committed `020c319` on `main`, pushed to origin.

**Validation, in two stages:**
1. Synthetic smoke test on disposable droplet `162.243.213.185`: seeded Oracle image + a stdlib mock OpenAI-compatible agent with a deliberate global-lock bottleneck (simulating single-GPU serialization). Correctly detected it (agent p95 grew 7.94x, DB flat). Caught and fixed a real bug in the process: throughput was computed from per-request offsets captured at task *submission* time, which don't reflect executor queueing delay once `total_requests > level` — fixed to measure the ThreadPoolExecutor block's actual wall-clock span directly.
2. Real run against a real (if resource-constrained) agent: local Ollama serving `qwen2.5-coder:1.5b` (the local machine has ~8GB RAM total, cannot load `qwen2.5-coder:14b` — confirmed via a hung 180s+ generate call before switching models), DB = seeded Oracle image on the disposable droplet (`--db-dsn` pointed cross-host — confirms DB doesn't need to be co-located with the agent). Levels 1/2/4/8, 10 rounds/level, static context strategy, 150 requests total.
  - **Result: correctly flagged an agent-bound bottleneck.** Throughput scaling efficiency ~13% (1.08x actual vs 8x ideal from level 1→8); agent p95 grew 7.5x (7.4s→55.5s) while DB p95 stayed under 4s throughout (2.25x growth); correctness also degraded under load (avg score 0.40→0.26) — consistent with a single Ollama worker queueing requests rather than parallelizing.
  - **Gotcha hit mid-run:** an earlier attempt at this same run was contaminated by a ~2.5hr real-time gap where the bash-terminal MCP connector became unresponsive (client-side tool timeout) — the laptop apparently idled/slept during that window, producing nonsense timings (`level_wall_time_s` of 1140s for 10 requests). Detected by eyeballing the numbers (real per-request latency had already been sanity-checked at ~5-16s via a manual curl test, so a 114s mean was an obvious red flag), killed the contaminated run, and restarted clean once the connector was confirmed responsive again. **Lesson: always sanity-check a fresh run's first-level numbers against an independent manual latency probe before trusting the full sweep, especially after any tool/connector interruption.**
- Wrote `docs/CONCURRENCY_BENCHMARK.md`: knowledge article covering what p50/p95/p99 mean (latency in seconds per-request-stage, not throughput; throughput is a separate per-level req/s figure), script usage, and a short analysis of the real run above, with an explicit caveat that N=10-80 requests/level is enough to demonstrate the harness works but too small for p99 to be a stable tail estimate (want ≥20 requests above the percentile threshold itself).
- Test infra cleanup: disposable droplet's `oracle-free` container removed after each validation pass; droplet itself left running (idle, Docker installed, no other state) for future reuse — same disposition as prior sessions.

**Server status at end of session:**
- Original target `162.243.121.249`: unreachable (per user, at session start). Not used.
- Disposable test droplet `162.243.213.185`: reachable, Docker installed, idle (no containers running).
- Local machine: Ollama running with `qwen2.5-coder:1.5b` and `qwen2.5-coder:14b` pulled (14b unusable here — insufficient RAM).

## Key learnings & principles
- **bash-terminal MCP tools only** reach the real machine; Claude's sandbox `create_file`/`str_replace` silently write to an isolated container instead — always verify a "file created" claim by reading it back via `bash-terminal:read_file` or `grep` over ssh/bash-terminal, especially right after switching tool families mid-conversation.
- **Sanity-check first-level results against an independent manual probe** before trusting a full concurrency/load-test sweep — tool/connector interruptions (MCP crash, laptop sleep) produce numbers that look superficially plausible (JSON parses fine) but are wall-clock garbage.
- **DB and agent-under-test don't need to be co-located** — `--db-dsn` on the concurrency harness works fine pointed at a different host than the agent, useful when the local machine can't run both.
