"""
Phase 4 — Benchmarking harness.

Runs every (non-MERGE) test case against every model x every context strategy
(static full-schema vs RAG-augmented), executes the generated SQL against Oracle,
scores it against a freshly-executed gold query, and logs everything to CSV/JSON.

Excludes sales_3_06 and hr_3_08 (MERGE statements) per the Phase 3 design note -
they are not idempotent and are out of scope for this automated result-based sweep.

Agent-under-test is pluggable (see scripts/agents/) - set AGENT_TYPE to point this
at Ollama, vLLM (or any OpenAI-compatible server), or an external hosted API
without touching this file. See docs/AGENTS.md for a full walkthrough.
"""
import decimal
import datetime
import json
import os
import re
import sys
import time

import oracledb

# SCRIPTS_DIR: where rag_pipeline.py and agents/ live (needed for imports) and,
# by default, where test_cases.json / rag_corpus.json / results are read/written from.
SCRIPTS_DIR = os.environ.get("SCRIPTS_DIR", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)
from rag_pipeline import build_static_prompt, build_rag_prompt, retrieve
from agents import get_agent

# --- Agent under test (SQL generation) --------------------------------------
# AGENT_TYPE: "ollama" (default) | "openai" (covers vLLM's OpenAI server, LM
# Studio, Together, Groq, the real OpenAI API, or anything else that speaks the
# standard /v1/chat/completions wire format).
# AGENT_URL: base URL for that agent - e.g. http://<host>:11434 for Ollama, or
# http://<host>:8000/v1 for vLLM (this is the multi-user-vllm project's default
# port), or https://api.openai.com/v1 for the real OpenAI API.
# AGENT_API_KEY: only needed for openai-type agents that actually check it
# (vLLM ignores it; hosted APIs require a real key).
# OLLAMA_URL is kept as a fallback for AGENT_URL so older docs/scripts that only
# set OLLAMA_URL still work unchanged when AGENT_TYPE=ollama (the default).
AGENT_TYPE = os.environ.get("AGENT_TYPE", "ollama")
AGENT_URL = os.environ.get("AGENT_URL", os.environ.get("OLLAMA_URL"))
AGENT_API_KEY = os.environ.get("AGENT_API_KEY")
agent = get_agent(AGENT_TYPE, base_url=AGENT_URL, api_key=AGENT_API_KEY)

MODELS = os.environ.get("MODELS", "qwen2.5-coder:32b,qwen2.5-coder:32b-instruct-q8_0").split(",")
CONTEXT_STRATEGIES = ["static", "rag"]
K_TABLES = 4   # tuned in 3.5.5
K_EXAMPLES = 2

# DB_DSN can point at a local Oracle container or a remote one (e.g. the server set
# up by this branch's setup_db.sh, if benchmark.py is run from a different host).
DB_USER = os.environ.get("DB_USER", "bench")
DB_PWD = os.environ.get("DB_PWD", "BenchmarkPwd123")
DB_DSN = os.environ.get("DB_DSN", "localhost:1521/FREEPDB1")

TEST_CASES_PATH = os.environ.get("TEST_CASES_PATH", os.path.join(SCRIPTS_DIR, "..", "testcases", "test_cases.json"))
RAG_CORPUS_PATH = os.environ.get("RAG_CORPUS_PATH", os.path.join(SCRIPTS_DIR, "..", "testcases", "rag_corpus.json"))
RESULTS_DIR = os.environ.get("RESULTS_DIR", os.path.join(SCRIPTS_DIR, "..", "testcases"))

EXCLUDED_IDS = {"sales_3_06", "hr_3_08"}  # non-idempotent MERGE statements


def load_test_cases():
    with open(TEST_CASES_PATH) as f:
        tc = json.load(f)
    return [t for t in tc if t["id"] not in EXCLUDED_IDS]


def load_corpus():
    with open(RAG_CORPUS_PATH) as f:
        return json.load(f)


def extract_sql(raw_text):
    """Strip markdown code fences / prose, return best-guess bare SQL statement."""
    text = raw_text.strip()
    # prefer fenced code block if present
    fence_match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence_match:
        text = fence_match.group(1).strip()
    # if there's leading prose before the first SQL keyword, cut it
    kw_match = re.search(
        r"\b(SELECT|WITH|MERGE|INSERT|UPDATE|DELETE)\b", text, re.IGNORECASE
    )
    if kw_match:
        text = text[kw_match.start():]
    # drop trailing prose after the final semicolon, if any semicolon exists
    if ";" in text:
        text = text[: text.rfind(";")]
    return text.strip().rstrip(";").strip()


def call_model(model, prompt):
    """Thin wrapper kept for readability at call sites below - delegates to
    whichever agent adapter AGENT_TYPE resolved to (see scripts/agents/)."""
    return agent.generate(model, prompt)


def serialize_value(v):
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if v is None:
        return None
    return str(v)


def _sort_key(row):
    # None sorts before any string, and we avoid comparing None to str directly
    return tuple((v is None, v if v is not None else "") for v in row)


def normalized_rows(rows):
    normalized = [tuple(serialize_value(v) for v in row) for row in rows]
    return sorted(normalized, key=_sort_key)


def run_sql(cur, sql):
    """Execute SQL, return (columns, rows, error) - error is None on success."""
    try:
        cur.execute(sql)
        if cur.description is None:
            return ["rows_affected"], [(cur.rowcount,)], None
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return columns, rows, None
    except Exception as e:
        return None, None, str(e)


def score(gold_rows, cand_columns, cand_rows, gold_error, cand_error):
    if cand_error is not None:
        return "error", 0.0
    if gold_error is not None:
        return "gold_error", 0.0
    gold_norm = normalized_rows(gold_rows)
    cand_norm = normalized_rows(cand_rows)
    if gold_norm == cand_norm:
        return "exact_match", 1.0
    # partial credit: fraction of gold rows present in candidate output
    gold_set = set(gold_norm)
    cand_set = set(cand_norm)
    if not gold_set:
        return "mismatch", 0.0
    overlap = len(gold_set & cand_set) / len(gold_set)
    return "mismatch", round(overlap, 3)


def main():
    test_cases = load_test_cases()
    corpus = load_corpus()
    print(f"Loaded {len(test_cases)} test cases (excluded {sorted(EXCLUDED_IDS)}), "
          f"corpus size {len(corpus)}")
    print(f"Agent under test: type={AGENT_TYPE} url={agent.base_url} models={MODELS}")

    conn = oracledb.connect(user=DB_USER, password=DB_PWD, dsn=DB_DSN)

    # resume support: skip (test_case_id, model, strategy) combos already completed
    results = []
    done = set()
    try:
        with open(os.path.join(RESULTS_DIR, "benchmark_results.json")) as f:
            results = json.load(f)
        done = {(r["test_case_id"], r["model"], r["context_strategy"]) for r in results
                if r.get("status") != "model_call_error"}
        print(f"Resuming: {len(done)} runs already completed, will skip those.")
    except FileNotFoundError:
        pass

    run_idx = len(results)
    total_runs = len(test_cases) * len(MODELS) * len(CONTEXT_STRATEGIES)

    for tc in test_cases:
        # gold rows re-executed fresh each time (cheap SELECTs; guards against drift)
        gold_cur = conn.cursor()
        gold_columns, gold_rows, gold_error = run_sql(gold_cur, tc["gold_sql"])
        gold_cur.close()

        for strategy in CONTEXT_STRATEGIES:
            if strategy == "static":
                prompt = build_static_prompt(tc["prompt"], tc["schema"])
                retrieved_tables, retrieved_examples = None, None
            else:
                top_tables, top_examples = retrieve(
                    tc["prompt"], corpus, k_tables=K_TABLES, k_examples=K_EXAMPLES,
                    exclude_test_case_id=tc["id"],
                )
                prompt = build_rag_prompt(tc["prompt"], top_tables, top_examples)
                retrieved_tables = sorted(t["table_name"] for t in top_tables)
                retrieved_examples = sorted(e["test_case_id"] for e in top_examples)

            for model in MODELS:
                if (tc["id"], model, strategy) in done:
                    continue
                run_idx += 1
                print(f"[{run_idx}/{total_runs}] {tc['id']:14s} model={model:32s} strategy={strategy}")
                try:
                    gen = call_model(model, prompt)
                except Exception as e:
                    results.append({
                        "test_case_id": tc["id"], "schema": tc["schema"], "tier": tc["tier"],
                        "model": model, "context_strategy": strategy,
                        "prompt_chars": len(prompt),
                        "status": "model_call_error", "error": str(e), "score": 0.0,
                    })
                    continue

                sql = extract_sql(gen["raw_text"])
                cand_cur = conn.cursor()
                cand_columns, cand_rows, cand_error = run_sql(cand_cur, sql)
                cand_cur.close()
                conn.commit()  # in case candidate SQL was accidentally DML

                status, sc = score(gold_rows, cand_columns, cand_rows, gold_error, cand_error)
                raw_metrics = gen.get("raw_metrics", {}) or {}

                results.append({
                    "test_case_id": tc["id"], "schema": tc["schema"], "tier": tc["tier"],
                    "model": model, "context_strategy": strategy,
                    "agent_provider": gen.get("provider", AGENT_TYPE),
                    "prompt_chars": len(prompt),
                    "retrieved_tables": retrieved_tables, "retrieved_examples": retrieved_examples,
                    "generated_sql": sql, "gold_sql": tc["gold_sql"],
                    "status": status, "score": sc, "sql_error": cand_error,
                    "gold_row_count": len(gold_rows) if gold_rows is not None else None,
                    "cand_row_count": len(cand_rows) if cand_rows is not None else None,
                    "wall_time_s": round(gen["wall_time_s"], 3),
                    "prompt_tokens": gen.get("prompt_tokens"),
                    "completion_tokens": gen.get("completion_tokens"),
                    "tokens_per_sec": round(gen["tokens_per_sec"], 2) if gen.get("tokens_per_sec") else None,
                    # provider-specific extras (e.g. Ollama's total/load/eval durations,
                    # or an OpenAI-compat agent's raw usage block) - kept as JSON so the
                    # flat CSV doesn't need a different schema per provider.
                    "raw_metrics_json": json.dumps(raw_metrics),
                })

                # checkpoint save every 10 runs in case of interruption
                if run_idx % 10 == 0:
                    with open(os.path.join(RESULTS_DIR, "benchmark_results.json"), "w") as f:
                        json.dump(results, f, indent=2)

    conn.close()
    with open(os.path.join(RESULTS_DIR, "benchmark_results.json"), "w") as f:
        json.dump(results, f, indent=2)

    # also write a flat CSV for quick inspection
    import csv
    keys = ["test_case_id", "schema", "tier", "model", "context_strategy", "agent_provider",
             "prompt_chars", "status", "score", "gold_row_count", "cand_row_count",
             "wall_time_s", "prompt_tokens", "completion_tokens", "tokens_per_sec",
             "raw_metrics_json", "sql_error"]
    with open(os.path.join(RESULTS_DIR, "benchmark_results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)

    print(f"\nDone. {len(results)} runs. Saved to {RESULTS_DIR}/benchmark_results.json and .csv")


if __name__ == "__main__":
    main()
