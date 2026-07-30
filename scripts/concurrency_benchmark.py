"""
Concurrency benchmark harness.

Companion to benchmark.py (which measures correctness + latency for ONE request
at a time, sweeping across models/strategies). This script holds the model and
context strategy FIXED and instead sweeps CONCURRENCY - firing an increasing
number of simultaneous NL->SQL requests at the agent-under-test and the DB, to
answer: "does correctness hold up, and how does response time degrade, as
concurrent load grows?"

At each concurrency level L (from CONCURRENCY_LEVELS):
  - L worker threads run in parallel (a ThreadPoolExecutor with max_workers=L)
  - together they fire L * ROUNDS_PER_LEVEL total requests (so higher
    concurrency levels also apply more total load - this mirrors how load
    tools like k6/Locust scale "virtual users")
  - each request: build prompt -> call agent (timed) -> run generated SQL
    against Oracle via a pooled connection (timed separately from the agent
    call, so we can tell agent-bound vs DB-bound slowdowns apart) -> score
    correctness against gold SQL, same scoring logic as benchmark.py.

Outputs (all under RESULTS_DIR, default ./testcases):
  concurrency_results.json / .csv   - one row per request (raw data)
  concurrency_summary.json / .csv   - one row per concurrency level (aggregates:
                                       throughput, latency percentiles, error
                                       rate, correctness rate)
  concurrency_report.md             - human-readable bottleneck analysis +
                                       recommendations, generated from the
                                       measured summary data (see
                                       analyze_bottlenecks() below) - not a
                                       static template, the findings are
                                       computed from the actual numbers.

Reuses scoring/prompt-building/agent-adapter code from benchmark.py /
rag_pipeline.py / agents/ so correctness scoring stays identical between the
two harnesses.
"""
import csv
import json
import os
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import oracledb

SCRIPTS_DIR = os.environ.get("SCRIPTS_DIR", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)
from rag_pipeline import build_static_prompt, build_rag_prompt, retrieve
from agents import get_agent
from benchmark import load_test_cases, load_corpus, extract_sql, run_sql, score

# --- Agent under test --------------------------------------------------------
AGENT_TYPE = os.environ.get("AGENT_TYPE", "ollama")
AGENT_URL = os.environ.get("AGENT_URL", os.environ.get("OLLAMA_URL"))
AGENT_API_KEY = os.environ.get("AGENT_API_KEY")
agent = get_agent(AGENT_TYPE, base_url=AGENT_URL, api_key=AGENT_API_KEY)

# Single fixed model + strategy - concurrency is the only variable under test here.
MODEL = os.environ.get("MODEL", os.environ.get("MODELS", "qwen2.5-coder:32b").split(",")[0])
CONTEXT_STRATEGY = os.environ.get("CONTEXT_STRATEGY", "static")  # "static" | "rag"
K_TABLES = 4
K_EXAMPLES = 2

CONCURRENCY_LEVELS = [int(x) for x in os.environ.get("CONCURRENCY_LEVELS", "1,2,4,8,16,32").split(",")]
ROUNDS_PER_LEVEL = int(os.environ.get("ROUNDS_PER_LEVEL", "3"))  # total reqs at level L = L * ROUNDS_PER_LEVEL
RANDOM_SEED = int(os.environ.get("RANDOM_SEED", "42"))

DB_USER = os.environ.get("DB_USER", "bench")
DB_PWD = os.environ.get("DB_PWD", "BenchmarkPwd123")
DB_DSN = os.environ.get("DB_DSN", "localhost:1521/FREEPDB1")

TEST_CASES_PATH = os.environ.get("TEST_CASES_PATH", os.path.join(SCRIPTS_DIR, "..", "testcases", "test_cases.json"))
RAG_CORPUS_PATH = os.environ.get("RAG_CORPUS_PATH", os.path.join(SCRIPTS_DIR, "..", "testcases", "rag_corpus.json"))
RESULTS_DIR = os.environ.get("RESULTS_DIR", os.path.join(SCRIPTS_DIR, "..", "testcases"))

# DB pool sized to the highest concurrency level we'll test, +2 headroom. Each
# worker thread acquires its own connection per request and releases it right
# after - connections are NOT shared across threads (python-oracledb
# connections aren't safe for concurrent use by multiple threads at once).
POOL_MAX = max(CONCURRENCY_LEVELS) + 2


def build_prompt_for(tc, corpus):
    """Returns prompt text. Built once per request (embedding call included in
    the 'rag' strategy's own timing, same as benchmark.py) - not shared/cached
    across requests, so concurrent RAG requests each pay their own embed cost,
    same as real traffic would."""
    if CONTEXT_STRATEGY == "static":
        return build_static_prompt(tc["prompt"], tc["schema"])
    top_tables, top_examples = retrieve(
        tc["prompt"], corpus, k_tables=K_TABLES, k_examples=K_EXAMPLES,
        exclude_test_case_id=tc["id"],
    )
    return build_rag_prompt(tc["prompt"], top_tables, top_examples)


def run_one_request(pool, tc, gold_cache, level, round_idx, req_idx):
    """Executes a single NL->SQL request end-to-end. Returns a result dict.
    Never raises - all failure modes (agent call, SQL execution, DB connection
    acquisition) are caught and recorded as a scored/errored result so one bad
    request doesn't take down the rest of the concurrency level."""
    t_req_start = time.time()
    row = {
        "concurrency_level": level, "round": round_idx, "request_idx": req_idx,
        "test_case_id": tc["id"], "schema": tc["schema"], "tier": tc["tier"],
        "model": MODEL, "context_strategy": CONTEXT_STRATEGY,
        "req_start_offset_s": None,  # filled in by caller relative to level start
    }
    try:
        prompt = build_prompt_for(tc, gold_cache["corpus"])
        row["prompt_chars"] = len(prompt)
    except Exception as e:
        row.update(status="prompt_build_error", error=str(e), score=0.0,
                    agent_wall_time_s=None, db_exec_time_s=None,
                    total_time_s=round(time.time() - t_req_start, 3))
        return row

    try:
        gen = agent.generate(MODEL, prompt)
        row["agent_wall_time_s"] = round(gen["wall_time_s"], 3)
        row["tokens_per_sec"] = round(gen["tokens_per_sec"], 2) if gen.get("tokens_per_sec") else None
    except Exception as e:
        row.update(status="model_call_error", error=str(e), score=0.0,
                    agent_wall_time_s=None, db_exec_time_s=None,
                    total_time_s=round(time.time() - t_req_start, 3))
        return row

    sql = extract_sql(gen["raw_text"])
    row["generated_sql"] = sql

    conn = None
    t_db_start = time.time()
    try:
        conn = pool.acquire()
        cur = conn.cursor()
        cand_columns, cand_rows, cand_error = run_sql(cur, sql)
        cur.close()
        conn.commit()
        row["db_exec_time_s"] = round(time.time() - t_db_start, 3)
    except Exception as e:
        row.update(status="db_error", error=str(e), score=0.0,
                    db_exec_time_s=round(time.time() - t_db_start, 3),
                    total_time_s=round(time.time() - t_req_start, 3))
        return row
    finally:
        if conn is not None:
            pool.release(conn)

    gold_columns, gold_rows, gold_error = gold_cache[tc["id"]]
    status, sc = score(gold_rows, cand_columns, cand_rows, gold_error, cand_error)
    row.update(status=status, score=sc, sql_error=cand_error,
                total_time_s=round(time.time() - t_req_start, 3))
    return row


def percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * (p / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return round(s[f] + (s[c] - s[f]) * (k - f), 3)


def summarize_level(level, rows, level_wall_time):
    n = len(rows)
    errors = [r for r in rows if r["status"] in ("model_call_error", "db_error", "prompt_build_error")]
    exact = [r for r in rows if r["status"] == "exact_match"]
    total_times = [r["total_time_s"] for r in rows if r.get("total_time_s") is not None]
    agent_times = [r["agent_wall_time_s"] for r in rows if r.get("agent_wall_time_s") is not None]
    db_times = [r["db_exec_time_s"] for r in rows if r.get("db_exec_time_s") is not None]
    # level_wall_time is passed in from main() as the actual measured wall-clock
    # span of the ThreadPoolExecutor block - NOT reconstructed from per-request
    # offsets, since req_start_offset_s is captured at *submission* time (near-
    # instant for all tasks) and does not reflect time spent queued inside the
    # executor waiting for a free worker when total_requests > level.

    return {
        "concurrency_level": level,
        "n_requests": n,
        "n_errors": len(errors),
        "error_rate": round(len(errors) / n, 3) if n else None,
        "avg_score": round(sum(r["score"] for r in rows) / n, 3) if n else None,
        "exact_match_rate": round(len(exact) / n, 3) if n else None,
        "throughput_req_s": round(n / level_wall_time, 3) if level_wall_time > 0 else None,
        "level_wall_time_s": round(level_wall_time, 3),
        "total_time_s": {
            "mean": round(statistics.mean(total_times), 3) if total_times else None,
            "p50": percentile(total_times, 50), "p95": percentile(total_times, 95),
            "p99": percentile(total_times, 99), "max": round(max(total_times), 3) if total_times else None,
        },
        "agent_wall_time_s": {
            "mean": round(statistics.mean(agent_times), 3) if agent_times else None,
            "p50": percentile(agent_times, 50), "p95": percentile(agent_times, 95),
        },
        "db_exec_time_s": {
            "mean": round(statistics.mean(db_times), 3) if db_times else None,
            "p50": percentile(db_times, 50), "p95": percentile(db_times, 95),
        },
    }


def analyze_bottlenecks(summaries):
    """Turns the measured per-level summaries into a findings + recommendations
    write-up. Every claim below is computed from the actual numbers in
    `summaries`, not boilerplate - thresholds are commented inline."""
    findings = []
    recs = []

    valid = [s for s in summaries if s["throughput_req_s"] is not None]
    if len(valid) < 2:
        return "Not enough successful concurrency levels to analyze scaling behavior.", []

    base = valid[0]
    top = valid[-1]

    # --- 1. Throughput scaling vs ideal linear scaling -----------------------
    ideal_ratio = top["concurrency_level"] / base["concurrency_level"]
    actual_ratio = (top["throughput_req_s"] / base["throughput_req_s"]) if base["throughput_req_s"] else 0
    scaling_efficiency = round(actual_ratio / ideal_ratio, 2) if ideal_ratio else 0
    findings.append(
        f"Throughput scaled {actual_ratio:.2f}x from concurrency={base['concurrency_level']} "
        f"({base['throughput_req_s']} req/s) to concurrency={top['concurrency_level']} "
        f"({top['throughput_req_s']} req/s) - {ideal_ratio:.0f}x concurrency increase would be "
        f"linear/ideal, so scaling efficiency is ~{scaling_efficiency*100:.0f}%."
    )
    if scaling_efficiency < 0.5:
        findings.append(
            "This is well below linear scaling (<50% efficiency), meaning the system is "
            "queueing/serializing work rather than handling requests truly in parallel."
        )

    # --- 2. Which stage dominates and grows fastest: agent vs DB -------------
    agent_p95_base = base["agent_wall_time_s"]["p95"] or 0
    agent_p95_top = top["agent_wall_time_s"]["p95"] or 0
    db_p95_base = base["db_exec_time_s"]["p95"] or 0
    db_p95_top = top["db_exec_time_s"]["p95"] or 0
    agent_growth = round(agent_p95_top / agent_p95_base, 2) if agent_p95_base else None
    db_growth = round(db_p95_top / db_p95_base, 2) if db_p95_base else None

    findings.append(
        f"p95 agent generation time: {agent_p95_base}s (concurrency={base['concurrency_level']}) -> "
        f"{agent_p95_top}s (concurrency={top['concurrency_level']})"
        + (f", {agent_growth}x growth." if agent_growth else ".")
    )
    findings.append(
        f"p95 DB execution time: {db_p95_base}s (concurrency={base['concurrency_level']}) -> "
        f"{db_p95_top}s (concurrency={top['concurrency_level']})"
        + (f", {db_growth}x growth." if db_growth else ".")
    )

    agent_dominant = agent_p95_top >= db_p95_top * 2 if db_p95_top else agent_p95_top > 0
    if agent_growth and agent_growth >= ideal_ratio * 0.6 and agent_dominant:
        findings.append(
            "Agent generation time is both the larger share of total latency AND growing "
            "roughly in step with concurrency - classic sign of requests queueing behind a "
            "single (or too few) model-serving worker rather than running in parallel."
        )
        recs.append(
            "Agent/model serving is the primary bottleneck. If serving via Ollama, note it "
            "typically processes one generation at a time per model instance on a single GPU "
            "- consider deploying multiple backend replicas behind a load-balancing router "
            "(see ../multi-user-vLLM/docs/ROUTER.md in this workspace) so concurrent requests "
            "actually run in parallel across GPUs/instances instead of queueing on one."
        )
        recs.append(
            "If serving via vLLM, check continuous-batching settings (e.g. max_num_seqs / "
            "max_num_batched_tokens) - vLLM can batch concurrent requests on one GPU far more "
            "efficiently than one-at-a-time serving, so a low ceiling there will look like this."
        )
    elif db_growth and db_growth >= ideal_ratio * 0.6 and not agent_dominant:
        findings.append(
            "DB execution time is growing roughly in step with concurrency and is a "
            "comparable-or-larger share of total latency than agent generation time - the "
            "Oracle side is a real contributor to the slowdown, not just the model server."
        )
        recs.append(
            "DB is a meaningful bottleneck under concurrency. Check: (a) the connection pool "
            "size actually used by the app vs. peak concurrency - undersized pools force "
            "requests to wait for a free connection; (b) Oracle's PROCESSES/SESSIONS init "
            "parameters aren't being hit; (c) whether generated queries are missing indexes "
            "on filtered/joined columns, which would show up as rising exec time under "
            "concurrent scan contention rather than flat exec time."
        )
    else:
        recs.append(
            "Neither stage clearly dominates the growth in this run - re-check with a wider "
            "concurrency sweep or more ROUNDS_PER_LEVEL for a cleaner signal; both agent and "
            "DB latency stayed roughly flat or grew sub-linearly, which is the desired outcome."
        )

    # --- 3. Error rate under load ---------------------------------------------
    error_rates = [(s["concurrency_level"], s["error_rate"]) for s in valid if s["error_rate"] is not None]
    rising_errors = [lvl for lvl, er in error_rates if er and er > 0.05]
    if rising_errors:
        findings.append(
            f"Error rate exceeded 5% at concurrency level(s): {rising_errors} "
            f"(vs. {error_rates[0][1]*100:.0f}% at concurrency={error_rates[0][0]})."
        )
        recs.append(
            "Errors climbing with concurrency usually means a hard resource ceiling is being "
            "hit (agent request-queue depth/timeout, DB connection pool exhaustion, or OS "
            "file-descriptor/ulimit caps) rather than a gradual slowdown. Check agent server "
            "logs and DB pool-wait stats at the levels flagged above; a queue that overflows "
            "outright (errors) is a harder failure than one that just adds latency."
        )
    else:
        findings.append("No concurrency level showed an error rate above 5% - failures were not the bottleneck here.")

    # --- 4. Correctness under load --------------------------------------------
    scores = [(s["concurrency_level"], s["avg_score"]) for s in valid if s["avg_score"] is not None]
    if scores:
        base_score, top_score = scores[0][1], scores[-1][1]
        if base_score and (base_score - top_score) > 0.10:
            findings.append(
                f"Correctness (avg score) degraded under load: {base_score} at "
                f"concurrency={scores[0][0]} -> {top_score} at concurrency={scores[-1][0]}."
            )
            recs.append(
                "Correctness dropping under concurrency (not just latency) suggests requests "
                "may be timing out or getting truncated responses under load - check the "
                "agent timeout config (AGENT timeout / vLLM request timeout) and whether "
                "generation is being cut off mid-query rather than genuinely failing to reason "
                "about harder questions."
            )
        else:
            findings.append(
                f"Correctness held steady under load: {base_score} at concurrency={scores[0][0]} "
                f"vs {top_score} at concurrency={scores[-1][0]} - correctness is not concurrency-sensitive here."
            )

    return findings, recs


def write_report(summaries, findings, recs, path):
    lines = [
        "# Concurrency Benchmark Report",
        "",
        f"Model: `{MODEL}` | Context strategy: `{CONTEXT_STRATEGY}` | "
        f"Levels: {[s['concurrency_level'] for s in summaries]} | "
        f"Rounds/level: {ROUNDS_PER_LEVEL}",
        "",
        "## Summary by concurrency level",
        "",
        "| Level | Requests | Errors | Avg Score | Exact Match | Throughput (req/s) | "
        "Total p50 (s) | Total p95 (s) | Agent p95 (s) | DB p95 (s) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        lines.append(
            f"| {s['concurrency_level']} | {s['n_requests']} | {s['n_errors']} | "
            f"{s['avg_score']} | {s['exact_match_rate']} | {s['throughput_req_s']} | "
            f"{s['total_time_s']['p50']} | {s['total_time_s']['p95']} | "
            f"{s['agent_wall_time_s']['p95']} | {s['db_exec_time_s']['p95']} |"
        )
    lines += ["", "## Findings", ""]
    lines += [f"- {f}" for f in findings]
    lines += ["", "## Recommendations", ""]
    if recs:
        lines += [f"- {r}" for r in recs]
    else:
        lines += ["- No specific bottleneck identified from this run's data."]
    lines += ["", f"_Generated by scripts/concurrency_benchmark.py, raw data in "
                   f"concurrency_results.json/.csv, per-level data in concurrency_summary.json/.csv._"]

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    random.seed(RANDOM_SEED)
    test_cases = load_test_cases()
    corpus = load_corpus() if CONTEXT_STRATEGY == "rag" else []
    print(f"Loaded {len(test_cases)} test cases. Agent: type={AGENT_TYPE} url={agent.base_url} "
          f"model={MODEL} strategy={CONTEXT_STRATEGY}")
    print(f"Concurrency levels: {CONCURRENCY_LEVELS}  rounds/level: {ROUNDS_PER_LEVEL}  "
          f"(total requests per level = level * rounds)")

    # gold rows computed once, up front, and reused across every concurrency
    # level/request (they don't depend on concurrency - just needed for scoring,
    # and re-running 500K-row SELECTs per request would itself become a
    # confound in the concurrency measurement).
    conn = oracledb.connect(user=DB_USER, password=DB_PWD, dsn=DB_DSN)
    gold_cache = {"corpus": corpus}
    print("Pre-computing gold results for all test cases...")
    for tc in test_cases:
        cur = conn.cursor()
        gold_cache[tc["id"]] = run_sql(cur, tc["gold_sql"])
        cur.close()
    conn.close()

    pool = oracledb.create_pool(user=DB_USER, password=DB_PWD, dsn=DB_DSN,
                                 min=2, max=POOL_MAX, increment=2)
    print(f"DB pool created (min=2, max={POOL_MAX})")

    all_rows = []
    summaries = []
    os.makedirs(RESULTS_DIR, exist_ok=True)

    for level in CONCURRENCY_LEVELS:
        total_reqs = level * ROUNDS_PER_LEVEL
        # shuffled, reproducible workload for this level - sampled with
        # replacement so levels aren't limited by the 72-case test set size.
        workload = [random.choice(test_cases) for _ in range(total_reqs)]
        print(f"\n=== Concurrency level {level}: firing {total_reqs} requests "
              f"({ROUNDS_PER_LEVEL} rounds x {level} workers) ===")

        level_rows = []
        level_start = time.time()
        with ThreadPoolExecutor(max_workers=level) as executor:
            futures = {}
            for i, tc in enumerate(workload):
                offset = time.time() - level_start
                fut = executor.submit(run_one_request, pool, tc, gold_cache, level, i // level, i)
                futures[fut] = offset
            for fut in as_completed(futures):
                row = fut.result()
                row["req_start_offset_s"] = round(futures[fut], 3)
                level_rows.append(row)
                status_flag = "OK" if row["status"] == "exact_match" else row["status"]
                print(f"  [{len(level_rows)}/{total_reqs}] {row['test_case_id']:14s} "
                      f"{status_flag:16s} total={row.get('total_time_s')}s")

        all_rows.extend(level_rows)
        level_wall_time = time.time() - level_start
        summary = summarize_level(level, level_rows, level_wall_time)
        summaries.append(summary)
        print(f"  -> throughput={summary['throughput_req_s']} req/s  "
              f"avg_score={summary['avg_score']}  error_rate={summary['error_rate']}  "
              f"total_p95={summary['total_time_s']['p95']}s")

        # checkpoint after every level in case of interruption
        with open(os.path.join(RESULTS_DIR, "concurrency_results.json"), "w") as f:
            json.dump(all_rows, f, indent=2)
        with open(os.path.join(RESULTS_DIR, "concurrency_summary.json"), "w") as f:
            json.dump(summaries, f, indent=2)

    pool.close()

    # flat CSVs
    if all_rows:
        keys = ["concurrency_level", "round", "request_idx", "test_case_id", "schema", "tier",
                 "model", "context_strategy", "req_start_offset_s", "prompt_chars", "status",
                 "score", "generated_sql", "sql_error", "agent_wall_time_s", "db_exec_time_s",
                 "total_time_s", "tokens_per_sec", "error"]
        with open(os.path.join(RESULTS_DIR, "concurrency_results.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for r in all_rows:
                w.writerow(r)

    if summaries:
        flat_summary_keys = ["concurrency_level", "n_requests", "n_errors", "error_rate",
                              "avg_score", "exact_match_rate", "throughput_req_s", "level_wall_time_s"]
        with open(os.path.join(RESULTS_DIR, "concurrency_summary.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(flat_summary_keys + ["total_p50", "total_p95", "total_p99",
                                             "agent_p95", "db_p95"])
            for s in summaries:
                w.writerow([s[k] for k in flat_summary_keys] + [
                    s["total_time_s"]["p50"], s["total_time_s"]["p95"], s["total_time_s"]["p99"],
                    s["agent_wall_time_s"]["p95"], s["db_exec_time_s"]["p95"],
                ])

    findings, recs = analyze_bottlenecks(summaries)
    report_path = os.path.join(RESULTS_DIR, "concurrency_report.md")
    write_report(summaries, findings, recs, report_path)

    print(f"\nDone. {len(all_rows)} requests across {len(CONCURRENCY_LEVELS)} concurrency levels.")
    print(f"Results: {RESULTS_DIR}/concurrency_results.{{json,csv}}")
    print(f"Summary: {RESULTS_DIR}/concurrency_summary.{{json,csv}}")
    print(f"Report:  {report_path}")


if __name__ == "__main__":
    main()
