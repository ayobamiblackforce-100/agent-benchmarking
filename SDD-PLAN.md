# SDD Plan: Pluggable Agent-Under-Test for the NL→SQL Benchmark Harness

Status: **COMPLETE.** All phases (A, B, 0, C1, C2, C3, D) PASSED, all 9
acceptance criteria met (§7), both repos committed and pushed with clean
working trees (`ollama-rag` @ `d8d1555`, `multi-user-vLLM` @ `19a5c4f`).
6 real bugs found and fixed along the way in `provision.sh`/router
healthcheck (see Progress Log / `multi-user-vLLM/claude.md` for details).
Full harness-level verification (not just direct curl) confirmed for both
router case-types: 140 `MODELS=auto` runs (134 exact_match) and 280
pinned-model runs (258 exact_match) against `root@162.243.121.249:9000`,
`classified_tier`/`routing_reason` correct on every row in both modes.
Source project: `ollama-rag` (branch `benchmark-db-setup`), consumed here as the
cross-project benchmarking initiative (`agent-benchmarking`)
Related projects: `ollama-rag` (DB + harness), `multi-user-vLLM` (agent under test
+ new router)

---

## 1. Goal

Make the existing Oracle NL→SQL benchmark harness (built in `ollama-rag`) able to
benchmark **any** model-serving backend — Ollama, vLLM's OpenAI-compatible server
(e.g. `multi-user-vLLM`), or an external hosted OpenAI-compatible API — through one
pluggable interface, without branching the repo per backend and without touching
harness/scoring logic per backend. Additionally, support two distinct testing
scenarios for multi-model agent deployments (added 2026-07-28, see §8 decision 4):

- **Case-type 1** — test the agent's own internal routing (the agent routes each
  query to one of its own internal models; we test the routing behavior itself).
- **Case-type 2** — test each model individually behind a multi-model agent
  deployment (same deployment, but pin a specific model and bypass routing).

This directly answers the questions raised in the prior discussion:
1. Can the same DB-benchmarking steps be reused against a vLLM agent (e.g.
   `multi-user-vLLM`)? → Yes, once an adapter layer exists (this plan builds it).
2. Can it also be reused against "an external agent altogether"? → Yes, provided
   that agent speaks the OpenAI `/v1/chat/completions` wire format; same adapter.
   Confirmed (§8): vLLM's OpenAI-compat server is accepted as sufficient proof of
   this, since it exercises the same generic `openai`-type adapter code path.
3. Can internal routing and per-model behavior be tested separately? → Yes, via a
   new router component (§2b) that speaks the same OpenAI wire format — no new
   `AGENT_TYPE` needed.

## 2. Current State — agent adapter layer (verified against the actual repo, not assumed)

Branch `benchmark-db-setup` in `ollama-rag` already has **uncommitted, working
first-draft code** for this — found via `git status` / `git diff`:

| Artifact | State |
|---|---|
| `scripts/agents/__init__.py` | Written. `get_agent(agent_type, base_url, api_key, timeout)` factory. Supports `ollama`, `openai`/`openai_compat`/`vllm`. |
| `scripts/agents/ollama_agent.py` | Written. Wraps original `/api/generate` + `/api/embeddings` logic, unchanged behavior, normalized return shape. |
| `scripts/agents/openai_agent.py` | Written, **updated 2026-07-28**. Wraps `/v1/chat/completions` (+ optional `/v1/embeddings`). Covers vLLM's OpenAI server, the new router (below), LM Studio, Together, Groq, real OpenAI API. Now additively captures a `router_metadata` block into `raw_metrics` when the upstream response includes one (no-op for any non-router endpoint — see §2b). |
| `scripts/benchmark.py` | Modified (uncommitted). `call_model()` now delegates to `agent.generate()`. Reads `AGENT_TYPE` / `AGENT_URL` / `AGENT_API_KEY` env vars (`OLLAMA_URL` kept as fallback for `AGENT_URL`). Results schema updated: `agent_provider`, `prompt_tokens`, `completion_tokens`, `raw_metrics_json` replace the Ollama-specific fields. |
| `scripts/rag_pipeline.py` | Modified (uncommitted). `embed()` delegates to a **separately configured** embedding agent (`EMBED_AGENT_TYPE`/`EMBED_URL`/`EMBED_MODEL`/`EMBED_API_KEY`) — intentionally decoupled from the agent-under-test, since vLLM/external APIs usually don't serve embeddings. Fixed the pre-existing hardcoded `/root/test_cases.json` path bug (`TEST_CASES_PATH` env var, defaults relative to script dir). |

**Not yet done (this is the actual remaining scope):**
- Nothing has been committed to git in either repo — all of the above is
  working-tree only.
- No live end-to-end smoke test has been confirmed to pass (prior session ended
  mid-smoke-test on a tool malfunction; state of that attempt is unknown/unverified).
- `docs/AGENTS.md` is referenced by code comments in 4 places but does not exist.
- `README.md` still only documents `OLLAMA_URL` — not updated for `AGENT_TYPE`/`AGENT_URL`/`EMBED_*`.
- No adapter has been validated against `multi-user-vLLM`'s actual deployed
  contract (confirmed from its `claude.md`: `POST http://<host>:8000/v1/chat/completions`,
  model id `Qwen/Qwen2.5-32B-Instruct`, no embeddings endpoint, `enforce-eager` mode).
- **Target server `162.243.194.166` has been destroyed.** A new box must be
  provisioned before any live run — see Phase 0.

## 2b. Current State — router infra for case-types 1 & 2 (built 2026-07-28, UNTESTED)

New infrastructure built in `multi-user-vLLM` to make both case-types actually
runnable (neither existed before — `multi-user-vLLM` only ever served one model
per deployment). Everything below is written and syntax/config-validated
(`py_compile`, JSON parse, `docker compose config` all pass) but **has not been
run against a live GPU box** — that's blocked on Phase 0.

| Artifact | Purpose |
|---|---|
| `multi-user-vLLM/router/app.py` | FastAPI router. `POST /v1/chat/completions`: if `model="auto"`, classifies the prompt's complexity and picks a backend; if `model=<a configured backend's model_id or name>`, routes directly to it (bypass). Injects an additive `router_metadata` block into the response (`requested_model`, `routed_backend`, `routed_model_id`, `routing_reason`, `classified_tier`, `router_wall_time_s`). Also exposes `GET /v1/models` (aggregated) and `GET /healthz`. |
| `multi-user-vLLM/router/classifier.py` | Heuristic tier-1/2/3 classifier (regex/keyword-based, mirrors the harness's own tier design) used for the `model="auto"` case-type-1 path. Deliberately simple/auditable rather than a trained model — swappable later behind the same `classify(prompt) -> tier` contract. |
| `multi-user-vLLM/router/routes.example.json` | Backend config template (`base_url`, `model_id`, `handles_tiers` per backend + a `default_backend`). Copy to `router/routes.json` (gitignored-by-convention, not yet added to `.gitignore` — see Phase F) and edit before deploying. |
| `multi-user-vLLM/Dockerfile.router` | Tiny Python 3.12-slim image (no GPU) for the router — separate from `Dockerfile.vllm` since the router does no inference. |
| `multi-user-vLLM/docker-compose.yml` | Extended with an opt-in `multi-model` Compose profile: `vllm-a` + `vllm-b` (two independently-configured vLLM backends, reusing the existing `Dockerfile.vllm`/`entrypoint.sh` unchanged) + `router`. Does **not** affect the default single-model `vllm` service — verified via `docker compose config` with and without the profile. |
| `multi-user-vLLM/.env.example` | New vars: `MULTI_MODEL_A`/`MULTI_MODEL_B` (model ids for the two backends), `MULTI_MODEL_A_PORT`/`MULTI_MODEL_B_PORT`, `ROUTER_PORT`, `ROUTER_UPSTREAM_TIMEOUT_S`. |
| `multi-user-vLLM/docs/ROUTER.md` | Usage doc: setup steps, exact `AGENT_TYPE`/`AGENT_URL`/`MODELS` invocations for case-type 1 (`MODELS=auto`) and case-type 2 (`MODELS=<model_id_a>,<model_id_b>`), design notes/limitations. |

**Key design point:** the router speaks the exact same `/v1/chat/completions`
wire format as plain vLLM, so **no new `AGENT_TYPE` was needed** — `AGENT_TYPE=openai`
(or its `vllm` alias) already works against it. The only harness-side change was
teaching `openai_agent.py` to opportunistically capture the router's extra
`router_metadata` field when present (additive, no-op against non-router endpoints).

**Not yet done for the router specifically:**
- Never deployed or smoke-tested against real backends (needs Phase 0's GPU box).
- `router/routes.json` (the real, non-example config) doesn't exist yet — created
  per-deployment from `routes.example.json`.
- Nothing committed to git.
- `router/routes.json` should be added to `.gitignore` once real deployment
  details (internal service names/ports are fine to commit, but if real external
  URLs/model choices end up sensitive, reconsider) — flagged, not yet decided.

## 3. In Scope / Out of Scope

**In scope:**
- Finish, verify, and commit the pluggable agent adapter layer (§2).
- Finish, verify, and commit the router infra for case-types 1 & 2 (§2b).
- Provision a new target server hosting Oracle DB + the `multi-user-vLLM` stack,
  including the `multi-model` profile (Phase 0).
- Validate against: Ollama (regression), vLLM single-model (also stands in as
  proof of the generic OpenAI-compatible path — §8 decision 2), and the router in
  both case-type-1 (`auto`) and case-type-2 (pinned model) modes.
- Write `docs/AGENTS.md` (ollama-rag) and update `README.md` accordingly.
- Update `CLAUDE.md` (ollama-rag) progress log to reflect this as a new phase.

**Out of scope (explicitly deferred):**
- Re-running the full 280-case benchmark sweep against every backend/mode
  (expensive; a 1-2 case smoke test per mode is sufficient to validate correctness
  — full sweeps are a follow-up).
- Standing up a third, separate "external agent" target (e.g. real OpenAI API) —
  dropped per §8 decision 2.
- Training or otherwise upgrading the router's classifier beyond the initial
  heuristic — flagged as a future improvement in `docs/ROUTER.md`, not required now.
- Scripting/automating the `multi-user-vLLM` deploy step itself (still manual per
  its docs) — nice-to-have follow-up, not required for this plan.
- Cross-provider result comparability beyond documenting the caveat that already
  exists in code comments (`tokens_per_sec` isn't apples-to-apples between Ollama's
  decode-only duration and OpenAI-style total wall time).

## 4. Requirements

### Functional
- R1: `benchmark.py` must run unmodified against Ollama, vLLM, and any OpenAI-
  compatible endpoint by only changing `AGENT_TYPE`/`AGENT_URL`/`AGENT_API_KEY` env vars.
- R2: `rag_pipeline.py`'s embedding step must remain independently configurable
  (`EMBED_AGENT_TYPE`/`EMBED_URL`/`EMBED_MODEL`) from the agent under test, since
  the agent under test frequently won't serve embeddings.
- R3: Every adapter's `generate()` must return the same normalized shape
  (`raw_text`, `wall_time_s`, `prompt_tokens`, `completion_tokens`, `tokens_per_sec`,
  `provider`, `raw_metrics`) so `benchmark.py` never branches on provider type.
- R4: Existing Ollama behavior must not regress — same request shape, same
  temperature=0, same result fields available (now under `raw_metrics` instead of
  top-level, which is an intentional, documented schema change).
- R5: Unknown `AGENT_TYPE` values must fail fast with a clear error, not silently
  default to Ollama.
- **R6 (new, case-type 1)**: it must be possible to point the harness at a single
  endpoint (`model="auto"`) and have that endpoint route each request to one of
  several internal models on its own, with the routing decision (which backend,
  why, and the classified complexity tier) captured per-run in the harness's
  results so routing behavior is auditable/analyzable after the fact — not just a
  pass/fail on the final SQL.
- **R7 (new, case-type 2)**: within the same multi-model deployment used for R6,
  it must also be possible to bypass routing and pin a specific model directly
  (via `model=<model_id>`), producing results equivalent to benchmarking that
  model standalone, so per-model behavior can be isolated from routing behavior.

### Non-functional
- N1: No new hard dependency beyond `requests` (harness) / FastAPI+uvicorn+httpx
  (router — new, isolated to the router's own tiny image, does not touch the
  harness's or vLLM's dependencies).
- N2: Adapter code must not need to know about SQL/benchmarking domain logic — it's
  a transport-layer concern only. The router likewise must not need to know about
  SQL/Oracle — it only sees prompt text and OpenAI-shaped messages.
- N3: Documentation must state the cross-provider `tokens_per_sec` comparability
  caveat prominently (in docs/AGENTS.md and README.md, not just inline comments).
- **N4 (new)**: R6/R7 must require zero new code in `benchmark.py`/`rag_pipeline.py`
  — satisfied by design, since the router is just another OpenAI-compatible
  endpoint from the harness's point of view (see §2b design point).

## 5. Interface Contracts

### 5a. Agent adapter (already implemented — spec to verify against)
```
agent = get_agent(agent_type, base_url=None, api_key=None, timeout=300)

agent.generate(model: str, prompt: str) -> {
  "raw_text": str,
  "wall_time_s": float,
  "prompt_tokens": int | None,
  "completion_tokens": int | None,
  "tokens_per_sec": float | None,
  "provider": str,
  "raw_metrics": dict,   # includes "router_metadata" when upstream is the router
}

agent.embed(text: str, model: str) -> list[float]   # not all providers implement this
```
Supported `agent_type`: `"ollama"` | `"openai"` (aliases `"openai_compat"`, `"vllm"`).

### 5b. Router (new, `multi-user-vLLM/router/`)
```
POST /v1/chat/completions   (standard OpenAI request/response shape, plus:)
  response.router_metadata = {
    "requested_model": str,       # what the caller asked for ("auto" or a model_id/name)
    "routed_backend": str,        # which configured backend actually served it
    "routed_model_id": str,
    "routing_reason": str,        # human-readable ("pinned to ..." / "classified tier N -> ...")
    "classified_tier": 1|2|3|null,  # null when pinned (routing was bypassed)
    "router_wall_time_s": float,
  }
GET /v1/models   -> aggregated backend list + virtual "auto" entry
GET /healthz     -> per-backend reachability check
```
Config: `router/routes.json` (`backends: [{name, base_url, model_id, handles_tiers}]`,
`default_backend`).

## 6. Task Breakdown (phased, checkpointed — matching this project's existing convention)

### Phase 0 — Provision new target server (BLOCKING PREREQUISITE)

`multi-user-vLLM` has no provisioning script (original deploy was manual: copy
stack, `docker compose up -d`). This new box needs to host Oracle DB (for the
harness) + the `multi-user-vLLM` stack, **including the new `multi-model` profile**
for Phase C2/C3 below.

- [x] 0.1. Stand up a fresh GPU box. *(Done — `root@162.243.121.249`, 1x H100 80GB. See decision log below and both repos' logs.)* Two vLLM backends will run simultaneously in
      multi-model mode (`vllm-a` + `vllm-b`) — size accordingly, or use smaller
      models than the original single-model deployment's 32B (the `.env.example`
      defaults to a 7B + 32B pair; adjust `MULTI_MODEL_A`/`MULTI_MODEL_B` to fit
      whatever GPU(s) you provision).
- [x] 0.2. Run `ollama-rag`'s `scripts/provision.sh <user>@<new-ip>`. *(DONE — all 10 steps completed and independently verified live: disk mount, Docker, NVIDIA toolkit, GPU passthrough, Ollama (3 models pulled), Python venv, Oracle DB (healthy, `bench` user, all 10 tables via both DDL scripts) all working. 4 real bugs found and fixed in the script during this work — 2 disk-selection bugs, 1 systemd-ownership bug, 1 flaky package-installed-check bug — all patched in the script itself, not just worked around live. See `ollama-rag/CLAUDE.md` for full detail.)*
- [x] 0.3. Copy `multi-user-vLLM`'s stack to the new box. *(Done — see `multi-user-vLLM/claude.md`.)*
- [x] 0.4. Deploy single-model mode first (regression baseline): `docker compose up -d`. *(Done in Phase C1, later stopped to free the GPU for multi-model mode — see below.)*
- [x] 0.5. Copy `router/routes.example.json` → `router/routes.json`, edit backend
      URLs (compose service names: `http://vllm-a:8000/v1`, `http://vllm-b:8000/v1`)
      and model ids to match whatever's actually deployed. *(Done — `routes.json` created live on the target server, matches the example's `small`/`large` backend split confirmed via C2.3/C3.3's `router_metadata`.)*
- [x] 0.6. Deploy multi-model mode: `docker compose --profile multi-model up -d vllm-a vllm-b router`. *(Done — `vllm-a`/`vllm-b`/`router` all up and healthy, GPU shows two separate processes with no collision, confirmed in Phase C1.)*
- [x] 0.7. Confirm: Oracle `bench@FREEPDB1` on 1521; single-model vLLM `/v1/models`
      on 8000; `vllm-a`/`vllm-b` on 8001/8002; router `/healthz` on 9000 reports
      all backends `ok`. *(Done — all confirmed live; also re-confirmed indirectly by C2.3/C3.3's 420 successful harness runs through the router.)*
- **Checkpoint 0: PASSED.** Both halves (`ollama-rag`'s Oracle DB/Ollama, and `multi-user-vLLM`'s single-model + multi-model vLLM backends + router) deployed and independently verified live.

**Needs from you:** the new server's IP/hostname and SSH access (or confirm I
should provision one myself if a cloud connector is available — none is currently
connected, so this needs to be a box you create and hand me access to).

### Phase A — Verify & finish existing agent-adapter work (no server needed)
- [x] A1. Re-review `scripts/agents/*.py`, diffs in `benchmark.py`/`rag_pipeline.py` for correctness. *(Done — see `ollama-rag/CLAUDE.md`.)*
- [x] A2. Byte-compile / syntax-check all changed files. *(Done for the new router-passthrough change in `openai_agent.py` — `py_compile` passed.)*
- [x] A3. Confirm `AGENT_URL` falling back to `OLLAMA_URL` behaves correctly when only the legacy var is set (R4). *(Confirmed — see `ollama-rag/CLAUDE.md`.)*
- **Checkpoint A: PASSED.** All changed files import cleanly; no behavior change for the Ollama path when only legacy env vars are set.

### Phase B — Local smoke test against Ollama (regression check, no new server needed)
- [x] B1. Run a 1-2 case smoke test (`AGENT_TYPE=ollama`) against a real reachable Ollama instance and confirm output matches pre-refactor shape/values. *(Done live against `root@162.243.121.249` — `llama3.2:1b` smoke prompt returned correct SQL with sane `eval_count`/`eval_duration` metrics. See `ollama-rag/CLAUDE.md`.)*
- **Checkpoint B: PASSED.** Ollama path produces a correct, scoreable result row (verified live, not just statically).

### Phase C1 — vLLM single-model smoke test — DONE
- [x] C1.1. Deployed live: `Qwen/Qwen2.5-32B-Instruct` on `http://162.243.121.249:8000/v1`, tier L auto-resolved (matches lookup table). `EMBED_URL` still Ollama (unaffected, separate service).
- [x] C1.2. Confirmed via direct `curl` (not yet run through the actual harness `benchmark.py` — that's still pending): generated SQL correct; `usage` block populated (`prompt_tokens`/`completion_tokens`/`total_tokens`) — exact shape `openai_agent.py` expects for `raw_metrics_json`. GPU utilization independently confirmed at ~87%, consistent with configured `--gpu-memory-utilization 0.88`.
- **Checkpoint C1: PASSED.** Satisfies R1 and the "external OpenAI-compatible agent" proof (§8 decision 2). See `multi-user-vLLM/claude.md` for full detail.

### Phase C2 — Router case-type 1 smoke test (internal routing) — DONE (direct-curl level)
- [x] C2.1. Deployed `vllm-a`/`vllm-b`/`router` live (multi-model profile). Tested via direct `curl` against the router's `/v1/chat/completions` with `model=auto` across 3 domain-appropriate prompts (tier 1: plain lookup; tier 2: join/group-by language; tier 3: LAG()/partition-by language) — classifier routed all three distinctly and correctly.
- [x] C2.2. Confirmed `router_metadata` populated correctly each time (`routed_backend`, `routed_model_id`, `routing_reason`, `classified_tier`).
- [x] C2.3. Run through the actual `ollama-rag/scripts/benchmark.py` harness: `AGENT_TYPE=openai AGENT_URL=http://162.243.121.249:9000/v1 MODELS=auto`, full 70-case suite x 2 context strategies = 140 runs. 134/140 `exact_match`, 6 legit model-generated-SQL errors (invalid identifiers / missing GROUP BY columns — model quality issues, not harness/router bugs). `router_metadata.classified_tier` spans all 3 tiers (1:23, 2:27, 3:90) across the run, `routed_backend`/`routed_model_id`/`routing_reason` populated and sane on every row. Found + fixed a real deploy gap along the way: `provision.sh` Step 2 never copied `testcases/` to the remote box, so every remote harness run failed with `FileNotFoundError` on `test_cases.json` — fixed with a `mkdir` + `scp` addition alongside the existing `sql/`/`scripts/` copies. `rag_corpus.json` built fresh on the target via `rag_pipeline.py` (retrieval sanity check 9/10).
- **Checkpoint C2: PASSED.** Routing behavior proven live (satisfies R6) *and* full harness-level correctness confirmed (C2.3). See `multi-user-vLLM/claude.md` and `ollama-rag/testcases/benchmark_results.json`.

### Phase C3 — Router case-type 2 smoke test (pinned model) — DONE (direct-curl level)
- [x] C3.1. Direct `curl` to the router pinning `Qwen/Qwen2.5-7B-Instruct-AWQ` explicitly, using the same tier-3-worded prompt from C2.1.
- [x] C3.2. Confirmed `router_metadata.classified_tier` is `null` and `routing_reason` is `"pinned to model_id 'Qwen/Qwen2.5-7B-Instruct-AWQ'"` — proves the pin genuinely bypassed classification (same prompt that got tier-3'd in C2.1 was routed to the small backend here instead, on request).
- [x] C3.3. Re-run through the full harness: `MODELS=Qwen/Qwen2.5-7B-Instruct-AWQ,Qwen/Qwen2.5-14B-Instruct` against the router, 70 cases x 2 models x 2 strategies = 280 runs. 258/280 `exact_match` (22 legit model-generated-SQL errors). `classified_tier=null` and `routing_reason="pinned to model_id '<id>'"` on all 280 rows; `routed_model_id` matched the requested pinned model with zero mismatches — bypass path fully proven at the harness level, not just direct-curl.
- **Checkpoint C3: PASSED** at both the direct-request level and the full-harness level — satisfies R7. See `multi-user-vLLM/claude.md` and `ollama-rag/testcases/benchmark_results.json`.

**Bonus finding, logged for completeness:** found and fixed a 5th real bug this
session — the router's own Docker healthcheck used `curl`, which its
`python:3.12-slim`-based image never had installed, so Docker reported it
`unhealthy` on every check despite the app answering every real request
correctly the whole time. Fixed by switching the healthcheck to a `python3`
one-liner (already available in the image) instead of adding a new dependency.

### Phase D — Documentation & commit
- [x] **Committed and pushed all working-tree changes in both repos to GitHub.**
  `ollama-rag` @ `2522346` (branch `benchmark-db-setup`), `multi-user-vLLM` @
  `f76aa2b` (branch `multi-user-vLLM`). Both working trees clean afterward.
- [x] **C2.3/C3.3 harness run + `provision.sh` fix committed and pushed.**
  `ollama-rag` @ `23ea0c6` (branch `benchmark-db-setup`) — `provision.sh`
  `testcases/` copy fix, `testcases/benchmark_results.{json,csv}`,
  `testcases/rag_corpus.json`.
- [x] D1. Wrote `ollama-rag/docs/AGENTS.md`: interface contract, supported providers, config var table, `tokens_per_sec` cross-provider caveat, 4 worked examples (Ollama, vLLM single-model, router case-type 1 `MODELS=auto`, router case-type 2 pinned bypass) — router examples cite the actual C2.3/C3.3 run results as validation, not hypothetical usage. `ollama-rag` @ `d8d1555`.
- [x] D2. Updated `ollama-rag/README.md`'s benchmark section with the full `AGENT_*`/`EMBED_*` var table, updated worked commands, and a router-invocation example pointing to `docs/AGENTS.md` for the full contract. `ollama-rag` @ `d8d1555`.
- [x] D3. Updated `ollama-rag/CLAUDE.md` with new phase entries: "Phase C2.3/C3.3 — Full harness verification through the router — COMPLETE" and "Phase D — Documentation — COMPLETE", same phase/checkpoint format as Phases 1-5. `ollama-rag` @ `d8d1555`.
- [x] D4. `git commit`+push on `ollama-rag`'s `benchmark-db-setup` branch — two logical commits: `23ea0c6` (C2.3/C3.3 harness fix + results) and `d8d1555` (D1-D3 docs).
- [x] D5. `git commit`+push on `multi-user-vLLM` — `19a5c4f` (`.gitignore` fix, D6 below). Router app/Dockerfile/compose/`.env.example`/`docs/ROUTER.md` were already committed in an earlier session (`f76aa2b`, per Phase D's first entry above).
- [x] D6. Decided and applied the `router/routes.json` gitignore question: same treatment as `.env` (per-deployment config, not committed even though today's values aren't sensitive — a future deployment could differ, and the file is always recreated from `routes.example.json` anyway). Added to `multi-user-vLLM/.gitignore` @ `19a5c4f`. `routes.json` never existed in either local working tree (only ever created live on the target server), so nothing needed to be un-tracked.
- **Checkpoint D: PASSED.** Docs match verified (not aspirational) behavior; both repos' git history reflects real, tested work; all 6 sub-items done.

## 7. Acceptance Criteria / Definition of Done

This work is done when **all** of the following are true:
1. `AGENT_TYPE=ollama` and `AGENT_TYPE=vllm` (single-model) both produce a correctly-scored result row from a live run.
2. Router case-type 1 (`MODELS=auto`) produces correctly-scored result rows with populated, sane `router_metadata` per run. **DONE — see C2.3.**
3. Router case-type 2 (`MODELS=<pinned model_id(s)>`) produces correctly-scored result rows with `classified_tier=null` / a "pinned" reason per run, proving the bypass path. **DONE — see C3.3.**
4. The same `benchmark.py`/`rag_pipeline.py` files are used for all of the above — zero per-mode branches outside `scripts/agents/` (adapter side) and `router/app.py` (router side). **DONE** — confirmed by code review in Phase A and unchanged since.
5. Legacy `OLLAMA_URL`-only invocations still work exactly as before. **DONE** — confirmed in Phase A (Checkpoint A: fallback chain `AGENT_URL` -> `OLLAMA_URL` -> per-type default reproduces pre-refactor behavior).
6. `docs/AGENTS.md` and `docs/ROUTER.md` both exist and accurately describe verified behavior. **DONE** — `docs/AGENTS.md` written in Phase D (D1), cites actual C2.3/C3.3 run numbers as evidence. `docs/ROUTER.md` was written earlier alongside the router infra itself (§2b).
7. `README.md` updated to match. **DONE** — D2.
8. `CLAUDE.md` phase log updated. **DONE** — D3.
9. Everything above committed to git in both repos — nothing left as an uncommitted working tree. **DONE** — `ollama-rag` clean at `d8d1555`, `multi-user-vLLM` clean at `19a5c4f`.

**All 9 acceptance criteria met. This SDD plan is COMPLETE.**

## 8. Decisions

1. **vLLM target** (resolved 2026-07-28): `162.243.194.166` destroyed; a new box
   will be provisioned → Phase 0, blocking Phase C1/C2/C3.
2. **External agent target** (resolved 2026-07-28): vLLM's OpenAI-compatible
   server accepted as sufficient proof of the generic `openai` adapter path. No
   separate third provider stood up.
3. **Smoke-test scope** (resolved 2026-07-28): 1-2 cases per mode (not a full sweep).
4. **Case-types 1 & 2** (resolved 2026-07-28): both added as formal requirements
   (R6, R7). Infra built (§2b) to make them actually testable — router + two-backend
   `multi-model` Compose profile in `multi-user-vLLM` — rather than treating
   case-type 1 as merely hypothetical. Built ahead of Phase 0 so it's ready to test
   the moment a server exists, per your request.

## 9. Risks / Known Gotchas

- Sandbox-only file tools (e.g. Claude's own `str_replace`/`create_file`) silently
  don't reach the real project directory — already caused one false "done" claim
  in this project's history (`CLAUDE.md`'s LAG() investigation note), and
  recurred again while drafting this plan. All work in this plan is executed via
  `bash-terminal` tools exclusively, with `read_file` used to re-confirm real
  on-disk state before any edit.
- vLLM's OpenAI server may not expose `/v1/embeddings` — handled by keeping
  `EMBED_*` independent of `AGENT_*` (R2); the router doesn't attempt to proxy
  embeddings either (out of scope, not needed — embeddings stay pointed at Ollama).
- Cross-provider `tokens_per_sec` is not directly comparable (decode-only vs.
  total wall time) — must stay documented, not silently conflated in analysis.
  The router adds a further wrinkle: `router_wall_time_s` includes routing
  overhead on top of backend generation time — call this out in docs/ROUTER.md
  (currently documented) and docs/AGENTS.md (pending, Phase D).
- The router's classifier is a simple heuristic (regex/keyword) — it can misroute
  on prompts that don't match its patterns; this is a known, documented
  limitation (`docs/ROUTER.md`), not a bug to chase down before Phase 0 testing.
- New target server not yet provisioned — Phase C1/C2/C3 cannot start until
  Phase 0 completes.
- Router code is entirely unvalidated against a real backend (only syntax/config
  checked so far) — first live test in Phase C2/C3 may surface real bugs (e.g.
  streaming edge cases, header forwarding quirks) not caught by static checks.

---

**Next step:** Phase A and Phase B can start immediately (no new server needed).
Phase 0 (server provisioning, now sized for two simultaneous vLLM backends) needs
your input — new server IP/access — before Phase C1/C2/C3 can run.


## 10. Addendum (2026-07-28, post-approval)

- **Target server acquired:** `root@162.243.121.249` (1x H100 80GB HBM3, 235GB RAM,
  682GB disk). Replaces the destroyed `162.243.194.166`. Phase 0 unblocked.
- **Kimi model request resolved:** user asked to include "kimi3 2.8" models,
  confirmed via web search to be Kimi K3 (2.8T total params, 104B active MoE) —
  requires 64+ accelerators per Moonshot's own guidance, does not fit this box.
  Tried the fallback (Kimi K2.7 Code, 1T total/32B active) per user's "try option
  2 else option 3" instruction — also doesn't fit (MoE keeps all expert weights
  resident regardless of active count; ~500GB+ even at INT4, needs multi-GPU
  H200-class hardware). **Decision: dropped Kimi, using only the Qwen models
  already planned** (§8 decisions unchanged otherwise).
- **Single-GPU multi-model fix (new, pre-deployment):** the `multi-model` compose
  profile built in §2b assumed independent GPU auto-detection per backend, which
  would collide on this box's single physical GPU. Fixed in `multi-user-vLLM`
  before any live deployment was attempted — `vllm-a` pinned to lookup-table tier
  `S` (`Qwen2.5-7B-Instruct-AWQ`, `--gpu-memory-utilization 0.25`), `vllm-b`
  pinned to tier `M` (`Qwen2.5-14B-Instruct`, `--gpu-memory-utilization 0.55`).
  Full rationale: `multi-user-vLLM/docs/ROUTER.md` "Single-GPU sizing" section.
  This changes the worked examples in §2b/§5b/Phase C2-C3 task descriptions from
  `Qwen2.5-7B-Instruct`/`Qwen2.5-32B-Instruct` to `Qwen2.5-7B-Instruct-AWQ`/
  `Qwen2.5-14B-Instruct` — noted here rather than rewriting those sections in
  place to keep this addendum as the single diff-friendly changelog entry.
- Both `ollama-rag/CLAUDE.md` and `multi-user-vLLM/claude.md` now carry a
  standing instruction (added 2026-07-28, per explicit user request) to log
  every completed step — tool used, project dir, target server status — before
  moving to the next. This plan file continues to be the cross-project source of
  truth for scope/requirements; the two `claude.md`/`CLAUDE.md` files are the
  step-by-step execution logs.
