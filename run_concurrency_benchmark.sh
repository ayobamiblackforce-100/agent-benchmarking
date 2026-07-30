#!/usr/bin/env bash
# run_concurrency_benchmark.sh — concurrency/load-test runner for agent-benchmarking.
#
# Companion to run_benchmark.sh. That script sweeps models/context-strategies one
# request at a time; this one holds model + strategy FIXED and sweeps CONCURRENCY,
# firing an increasing number of simultaneous NL->SQL requests at the agent under
# test (and Oracle) to answer: does correctness hold up, and how does response
# time degrade, as concurrent load grows? It also writes a bottleneck-analysis
# report (concurrency_report.md) computed from the measured numbers.
#
# Usage:
#   ./run_concurrency_benchmark.sh --agent-url http://localhost:12225 --model my-model-id
#   ./run_concurrency_benchmark.sh --agent-url http://localhost:11434 --agent-type ollama \
#       --model qwen2.5-coder:32b --levels 1,2,4,8,16 --rounds 5
#
# Run --help for the full option list.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- Defaults ----------------------------------------------------------------
AGENT_TYPE="openai"
AGENT_URL=""
AGENT_API_KEY="EMPTY"
MODEL=""

CONTEXT_STRATEGY="static"
CONCURRENCY_LEVELS="1,2,4,8,16,32"
ROUNDS_PER_LEVEL="3"
RANDOM_SEED="42"

DB_DSN="localhost:1521/FREEPDB1"
DB_USER="bench"
DB_PWD="BenchmarkPwd123"

# Only needed if --context-strategy rag is used (embeddings are called live,
# per request, same as run_benchmark.sh's rag strategy).
EMBED_AGENT_TYPE="ollama"
EMBED_URL="http://localhost:11434"
EMBED_MODEL="nomic-embed-text"
EMBED_API_KEY="EMPTY"

VENV_PATH="$SCRIPT_DIR/.venv"
TEST_CASES_PATH="$SCRIPT_DIR/testcases/test_cases.json"
RAG_CORPUS_PATH="$SCRIPT_DIR/testcases/rag_corpus.json"
RESULTS_DIR="$SCRIPT_DIR/testcases"

SKIP_CHECKS=0

usage() {
  cat <<EOF
Usage: $(basename "$0") --agent-url URL [options]

Required:
  --agent-url URL           Base URL of the agent under test, e.g. http://localhost:12225
                             (for --agent-type openai, "/v1" is appended automatically
                             if you don't include it)

Agent under test:
  --agent-type TYPE         openai (default, covers vLLM/router/LM Studio/hosted APIs) | ollama
  --model MODEL             Single model id/tag to load-test. Use "auto" for a router's
                             own internal routing. If omitted, auto-detected like
                             run_benchmark.sh (openai-type only).
  --agent-api-key KEY       Bearer token (default: EMPTY — fine for vLLM/router/Ollama)

Concurrency sweep:
  --levels LIST             Comma-separated concurrency levels (default: 1,2,4,8,16,32)
  --rounds N                Requests per worker at each level — total requests at
                             level L = L * N (default: 3)
  --context-strategy S      static (default) | rag — held fixed across the whole sweep
  --seed N                  Random seed for reproducible workload sampling (default: 42)

Database:
  --db-dsn DSN               default: localhost:1521/FREEPDB1
  --db-user USER              default: bench
  --db-pwd PWD                default: BenchmarkPwd123

Embeddings (only used if --context-strategy rag):
  --embed-agent-type TYPE   default: ollama
  --embed-url URL           default: http://localhost:11434
  --embed-model MODEL       default: nomic-embed-text
  --embed-api-key KEY       default: EMPTY

Paths:
  --venv-path PATH          default: ./.venv
  --test-cases-path PATH    default: ./testcases/test_cases.json
  --rag-corpus-path PATH    default: ./testcases/rag_corpus.json
  --results-dir PATH        default: ./testcases

Other:
  --skip-checks             Skip the pre-flight DB/agent reachability checks
  -h, --help                Show this help and exit

Outputs (under --results-dir):
  concurrency_results.json / .csv   one row per request
  concurrency_summary.json / .csv   one row per concurrency level (throughput,
                                     latency percentiles, error rate, correctness)
  concurrency_report.md             bottleneck findings + recommendations,
                                     computed from the measured numbers above

Examples:
  # vLLM / router / any OpenAI-compatible server, pinned model, default sweep
  $(basename "$0") --agent-url http://localhost:12225 --model Qwen/Qwen2.5-7B-Instruct-AWQ

  # Ollama, custom sweep
  $(basename "$0") --agent-url http://localhost:11434 --agent-type ollama \\
      --model qwen2.5-coder:32b --levels 1,2,4,8 --rounds 5
EOF
}

# ---- Arg parsing ---------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent-url) AGENT_URL="$2"; shift 2 ;;
    --agent-type) AGENT_TYPE="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --agent-api-key) AGENT_API_KEY="$2"; shift 2 ;;
    --levels) CONCURRENCY_LEVELS="$2"; shift 2 ;;
    --rounds) ROUNDS_PER_LEVEL="$2"; shift 2 ;;
    --context-strategy) CONTEXT_STRATEGY="$2"; shift 2 ;;
    --seed) RANDOM_SEED="$2"; shift 2 ;;
    --db-dsn) DB_DSN="$2"; shift 2 ;;
    --db-user) DB_USER="$2"; shift 2 ;;
    --db-pwd) DB_PWD="$2"; shift 2 ;;
    --embed-agent-type) EMBED_AGENT_TYPE="$2"; shift 2 ;;
    --embed-url) EMBED_URL="$2"; shift 2 ;;
    --embed-model) EMBED_MODEL="$2"; shift 2 ;;
    --embed-api-key) EMBED_API_KEY="$2"; shift 2 ;;
    --venv-path) VENV_PATH="$2"; shift 2 ;;
    --test-cases-path) TEST_CASES_PATH="$2"; shift 2 ;;
    --rag-corpus-path) RAG_CORPUS_PATH="$2"; shift 2 ;;
    --results-dir) RESULTS_DIR="$2"; shift 2 ;;
    --skip-checks) SKIP_CHECKS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "$AGENT_URL" ]]; then
  echo "ERROR: --agent-url is required." >&2
  usage
  exit 1
fi

if [[ "$CONTEXT_STRATEGY" != "static" && "$CONTEXT_STRATEGY" != "rag" ]]; then
  echo "ERROR: --context-strategy must be 'static' or 'rag', got '$CONTEXT_STRATEGY'." >&2
  exit 1
fi

# Normalize agent URL: openai-type servers speak /v1/chat/completions -
# auto-append /v1 if the user gave a bare host:port.
if [[ "$AGENT_TYPE" == "openai" && "$AGENT_URL" != */v1 ]]; then
  echo "Note: --agent-type openai expects a /v1 base URL; appending it: $AGENT_URL -> ${AGENT_URL%/}/v1"
  AGENT_URL="${AGENT_URL%/}/v1"
fi

echo "=== agent-benchmarking: concurrency sweep ==="
echo "  agent_type=$AGENT_TYPE  agent_url=$AGENT_URL  model=${MODEL:-<auto-detect>}"
echo "  context_strategy=$CONTEXT_STRATEGY  levels=$CONCURRENCY_LEVELS  rounds/level=$ROUNDS_PER_LEVEL"
echo "  db_dsn=$DB_DSN"
echo

# ---- 1. venv setup ---------------------------------------------------------------
if [[ ! -x "$VENV_PATH/bin/python3" ]]; then
  echo "--- Setting up venv at $VENV_PATH (oracledb, requests) ---"
  python3 -m venv "$VENV_PATH"
  "$VENV_PATH/bin/pip" install --quiet --upgrade pip
  "$VENV_PATH/bin/pip" install --quiet oracledb requests
else
  "$VENV_PATH/bin/pip" show oracledb >/dev/null 2>&1 || "$VENV_PATH/bin/pip" install --quiet oracledb
  "$VENV_PATH/bin/pip" show requests >/dev/null 2>&1 || "$VENV_PATH/bin/pip" install --quiet requests
fi
PY="$VENV_PATH/bin/python3"
echo "Using $PY"
echo

# ---- 2. pre-flight checks -----------------------------------------------------
if [[ "$SKIP_CHECKS" -eq 0 ]]; then
  echo "--- Checking DB reachability ($DB_DSN) ---"
  "$PY" -c "
import oracledb, sys
try:
    conn = oracledb.connect(user='$DB_USER', password='$DB_PWD', dsn='$DB_DSN')
    conn.cursor().execute('SELECT 1 FROM DUAL')
    print('OK: DB reachable.')
except Exception as e:
    print(f'FAILED: {e}', file=sys.stderr)
    sys.exit(1)
"
  echo

  echo "--- Checking agent reachability ($AGENT_URL) ---"
  if [[ "$AGENT_TYPE" == "openai" ]]; then
    MODELS_JSON="$(curl -sf "$AGENT_URL/models" || true)"
    if [[ -z "$MODELS_JSON" ]]; then
      echo "FAILED: could not reach $AGENT_URL/models — is the agent running?" >&2
      exit 1
    fi
    echo "OK: agent reachable. Models reported:"
    echo "$MODELS_JSON" | "$PY" -c "
import json, sys
data = json.load(sys.stdin)
for m in data.get('data', []):
    print(f\"  - {m.get('id')}\")"
    if [[ -z "$MODEL" ]]; then
      MODEL="$(echo "$MODELS_JSON" | "$PY" -c "
import json, sys
data = json.load(sys.stdin)
ids = [m.get('id') for m in data.get('data', []) if m.get('id') and m.get('id') != 'auto']
print(ids[0] if ids else '')")"
      if [[ -z "$MODEL" ]]; then
        echo "ERROR: --model not given and no usable model id found in $AGENT_URL/models. Pass --model explicitly." >&2
        exit 1
      fi
      echo "Note: --model not given, auto-selected: $MODEL"
    fi
  else
    if ! curl -sf "$AGENT_URL/api/tags" >/dev/null; then
      echo "FAILED: could not reach $AGENT_URL/api/tags — is Ollama running?" >&2
      exit 1
    fi
    echo "OK: agent reachable."
    if [[ -z "$MODEL" ]]; then
      echo "ERROR: --model is required for --agent-type ollama (no reliable default)." >&2
      exit 1
    fi
  fi
  echo

  if [[ "$CONTEXT_STRATEGY" == "rag" ]]; then
    if [[ ! -f "$RAG_CORPUS_PATH" ]]; then
      echo "ERROR: --context-strategy rag requires $RAG_CORPUS_PATH to already exist." >&2
      echo "  Run run_benchmark.sh once first (it builds the corpus), or build it directly via scripts/rag_pipeline.py." >&2
      exit 1
    fi
    echo "--- Checking embedding backend ($EMBED_AGENT_TYPE @ $EMBED_URL, model=$EMBED_MODEL) ---"
    EMBED_CHECK_ERR="$("$PY" -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR/scripts')
from agents import get_agent
try:
    a = get_agent('$EMBED_AGENT_TYPE', base_url='$EMBED_URL', api_key='$EMBED_API_KEY')
    v = a.embed('connectivity check', model='$EMBED_MODEL')
    assert isinstance(v, list) and len(v) > 0
except Exception as e:
    print(str(e))
    sys.exit(1)
" 2>&1)" && EMBED_CHECK_OK=1 || EMBED_CHECK_OK=0
    if [[ "$EMBED_CHECK_OK" -eq 0 ]]; then
      echo "FAILED: embed call to $EMBED_URL (model=$EMBED_MODEL) failed:" >&2
      echo "  $EMBED_CHECK_ERR" >&2
      exit 1
    fi
    echo "OK: embedding backend reachable and $EMBED_MODEL responds."
    echo
  fi
else
  if [[ -z "$MODEL" ]]; then
    echo "ERROR: --model is required when --skip-checks is set (no auto-detect possible)." >&2
    exit 1
  fi
fi

# ---- 3. run the concurrency sweep ----------------------------------------------
echo "--- Running concurrency_benchmark.py ---"
mkdir -p "$RESULTS_DIR"
DB_DSN="$DB_DSN" DB_USER="$DB_USER" DB_PWD="$DB_PWD" \
AGENT_TYPE="$AGENT_TYPE" AGENT_URL="$AGENT_URL" AGENT_API_KEY="$AGENT_API_KEY" MODEL="$MODEL" \
CONTEXT_STRATEGY="$CONTEXT_STRATEGY" CONCURRENCY_LEVELS="$CONCURRENCY_LEVELS" \
ROUNDS_PER_LEVEL="$ROUNDS_PER_LEVEL" RANDOM_SEED="$RANDOM_SEED" \
EMBED_AGENT_TYPE="$EMBED_AGENT_TYPE" EMBED_URL="$EMBED_URL" EMBED_MODEL="$EMBED_MODEL" EMBED_API_KEY="$EMBED_API_KEY" \
TEST_CASES_PATH="$TEST_CASES_PATH" RAG_CORPUS_PATH="$RAG_CORPUS_PATH" RESULTS_DIR="$RESULTS_DIR" \
  "$PY" "$SCRIPT_DIR/scripts/concurrency_benchmark.py"
echo

# ---- 4. summary ------------------------------------------------------------------
echo "--- Bottleneck report ---"
cat "$RESULTS_DIR/concurrency_report.md"
