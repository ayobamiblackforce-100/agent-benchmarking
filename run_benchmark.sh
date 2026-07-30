#!/usr/bin/env bash
# run_benchmark.sh — one-touch NL->SQL benchmark runner for agent-benchmarking.
#
# Wraps the Oracle-DB NL->SQL harness (scripts/benchmark.py + scripts/rag_pipeline.py,
# vendored in from ollama-rag) behind a single command: sets up a local venv if
# needed, sanity-checks the DB and the agent-under-test are reachable, builds the
# RAG corpus if missing, then runs the full benchmark sweep.
#
# See docs/AGENTS.md (also vendored in) for the full env-var contract this wraps.
#
# Usage:
#   ./run_benchmark.sh --agent-url http://localhost:12225 --models my-model-id
#   ./run_benchmark.sh --agent-url http://localhost:9000 --models auto        # router, case-type 1
#   ./run_benchmark.sh --agent-url http://localhost:11434 --agent-type ollama --models qwen2.5-coder:32b
#
# Run --help for the full option list.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- Defaults ----------------------------------------------------------------
AGENT_TYPE="openai"
AGENT_URL=""
AGENT_API_KEY="EMPTY"
MODELS=""

DB_DSN="localhost:1521/FREEPDB1"
DB_USER="bench"
DB_PWD="BenchmarkPwd123"

EMBED_AGENT_TYPE="ollama"
EMBED_URL="http://localhost:11434"
EMBED_MODEL="nomic-embed-text"
EMBED_API_KEY="EMPTY"

VENV_PATH="$SCRIPT_DIR/.venv"
TEST_CASES_PATH="$SCRIPT_DIR/testcases/test_cases.json"
RAG_CORPUS_PATH="$SCRIPT_DIR/testcases/rag_corpus.json"
RESULTS_DIR="$SCRIPT_DIR/testcases"

FORCE_REBUILD_CORPUS=0
SKIP_CORPUS_BUILD=0
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
  --models LIST             Comma-separated model id(s)/tag(s) to benchmark.
                             Use "auto" for a router's own internal routing (case-type 1).
                             If omitted, the script queries the agent's model list and
                             uses the first one it finds (openai-type only).
  --agent-api-key KEY       Bearer token (default: EMPTY — fine for vLLM/router/Ollama)

Database:
  --db-dsn DSN              default: localhost:1521/FREEPDB1
  --db-user USER            default: bench
  --db-pwd PWD              default: BenchmarkPwd123

Embeddings (for RAG corpus build — independent of the agent under test):
  --embed-agent-type TYPE   default: ollama
  --embed-url URL           default: http://localhost:11434
  --embed-model MODEL       default: nomic-embed-text
  --embed-api-key KEY       default: EMPTY
  --skip-corpus-build       Don't build/check the RAG corpus at all — fail if it's missing
  --force-rebuild-corpus    Rebuild testcases/rag_corpus.json even if it already exists

Paths:
  --venv-path PATH          default: ./.venv
  --test-cases-path PATH    default: ./testcases/test_cases.json
  --rag-corpus-path PATH    default: ./testcases/rag_corpus.json
  --results-dir PATH        default: ./testcases

Other:
  --skip-checks             Skip the pre-flight DB/agent reachability checks
  -h, --help                Show this help and exit

Examples:
  # vLLM / router / any OpenAI-compatible server on localhost:12225, pinned model
  $(basename "$0") --agent-url http://localhost:12225 --models Qwen/Qwen2.5-7B-Instruct-AWQ

  # router, test its own internal routing
  $(basename "$0") --agent-url http://localhost:9000 --models auto

  # Ollama
  $(basename "$0") --agent-url http://localhost:11434 --agent-type ollama --models qwen2.5-coder:32b
EOF
}

# ---- Arg parsing ---------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent-url) AGENT_URL="$2"; shift 2 ;;
    --agent-type) AGENT_TYPE="$2"; shift 2 ;;
    --models) MODELS="$2"; shift 2 ;;
    --agent-api-key) AGENT_API_KEY="$2"; shift 2 ;;
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
    --skip-corpus-build) SKIP_CORPUS_BUILD=1; shift ;;
    --force-rebuild-corpus) FORCE_REBUILD_CORPUS=1; shift ;;
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

# Normalize agent URL: openai-type servers speak /v1/chat/completions -
# auto-append /v1 if the user gave a bare host:port.
if [[ "$AGENT_TYPE" == "openai" && "$AGENT_URL" != */v1 ]]; then
  echo "Note: --agent-type openai expects a /v1 base URL; appending it: $AGENT_URL -> ${AGENT_URL%/}/v1"
  AGENT_URL="${AGENT_URL%/}/v1"
fi

echo "=== agent-benchmarking: one-touch run ==="
echo "  agent_type=$AGENT_TYPE  agent_url=$AGENT_URL  models=${MODELS:-<auto-detect>}"
echo "  db_dsn=$DB_DSN"
echo

# ---- 1. venv setup ---------------------------------------------------------------
if [[ ! -x "$VENV_PATH/bin/python3" ]]; then
  echo "--- Setting up venv at $VENV_PATH (oracledb, requests) ---"
  python3 -m venv "$VENV_PATH"
  "$VENV_PATH/bin/pip" install --quiet --upgrade pip
  "$VENV_PATH/bin/pip" install --quiet oracledb requests
else
  # make sure deps are present even if venv already existed from elsewhere
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
    if [[ -z "$MODELS" ]]; then
      MODELS="$(echo "$MODELS_JSON" | "$PY" -c "
import json, sys
data = json.load(sys.stdin)
ids = [m.get('id') for m in data.get('data', []) if m.get('id') and m.get('id') != 'auto']
print(ids[0] if ids else '')")"
      if [[ -z "$MODELS" ]]; then
        echo "ERROR: --models not given and no usable model id found in $AGENT_URL/models. Pass --models explicitly." >&2
        exit 1
      fi
      echo "Note: --models not given, auto-selected: $MODELS"
    fi
  else
    if ! curl -sf "$AGENT_URL/api/tags" >/dev/null; then
      echo "FAILED: could not reach $AGENT_URL/api/tags — is Ollama running?" >&2
      exit 1
    fi
    echo "OK: agent reachable."
    if [[ -z "$MODELS" ]]; then
      echo "ERROR: --models is required for --agent-type ollama (no reliable default)." >&2
      exit 1
    fi
  fi
  echo

  # The embedding backend is needed for every "rag"-strategy run (retrieve()
  # embeds each question live, not just once at corpus-build time), so check
  # it regardless of whether the corpus already exists / --skip-corpus-build.
  # This does a REAL embed call, not just a reachability ping - Ollama can be
  # up and reachable while missing the specific EMBED_MODEL, which /api/tags
  # alone won't catch.
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
    echo "  This is EMBED_URL/EMBED_MODEL, independent of --agent-url - needed for every" >&2
    echo "  'rag'-strategy run, not just building the corpus. If Ollama is up but missing" >&2
    echo "  the model: ollama pull $EMBED_MODEL — or point --embed-url at a host that" >&2
    echo "  already has it (e.g. wherever the existing rag_corpus.json was built)." >&2
    exit 1
  fi
  echo "OK: embedding backend reachable and $EMBED_MODEL responds."
  echo
else
  if [[ -z "$MODELS" ]]; then
    echo "ERROR: --models is required when --skip-checks is set (no auto-detect possible)." >&2
    exit 1
  fi
fi

# ---- 3. RAG corpus -------------------------------------------------------------
if [[ "$SKIP_CORPUS_BUILD" -eq 1 ]]; then
  if [[ ! -f "$RAG_CORPUS_PATH" ]]; then
    echo "ERROR: --skip-corpus-build set but $RAG_CORPUS_PATH does not exist." >&2
    exit 1
  fi
  echo "--- Skipping RAG corpus build (using existing $RAG_CORPUS_PATH) ---"
elif [[ -f "$RAG_CORPUS_PATH" && "$FORCE_REBUILD_CORPUS" -eq 0 ]]; then
  echo "--- RAG corpus already exists at $RAG_CORPUS_PATH, skipping build (use --force-rebuild-corpus to rebuild) ---"
else
  echo "--- Building RAG corpus via $EMBED_AGENT_TYPE @ $EMBED_URL ($EMBED_MODEL) ---"
  TEST_CASES_PATH="$TEST_CASES_PATH" RAG_CORPUS_PATH="$RAG_CORPUS_PATH" \
  EMBED_AGENT_TYPE="$EMBED_AGENT_TYPE" EMBED_URL="$EMBED_URL" EMBED_MODEL="$EMBED_MODEL" EMBED_API_KEY="$EMBED_API_KEY" \
    "$PY" "$SCRIPT_DIR/scripts/rag_pipeline.py"
fi
echo

# ---- 4. run the benchmark ------------------------------------------------------
echo "--- Running benchmark.py ---"
mkdir -p "$RESULTS_DIR"
DB_DSN="$DB_DSN" DB_USER="$DB_USER" DB_PWD="$DB_PWD" \
AGENT_TYPE="$AGENT_TYPE" AGENT_URL="$AGENT_URL" AGENT_API_KEY="$AGENT_API_KEY" MODELS="$MODELS" \
EMBED_AGENT_TYPE="$EMBED_AGENT_TYPE" EMBED_URL="$EMBED_URL" EMBED_MODEL="$EMBED_MODEL" EMBED_API_KEY="$EMBED_API_KEY" \
TEST_CASES_PATH="$TEST_CASES_PATH" RAG_CORPUS_PATH="$RAG_CORPUS_PATH" RESULTS_DIR="$RESULTS_DIR" \
  "$PY" "$SCRIPT_DIR/scripts/benchmark.py"
# ^ NOTE: EMBED_* must be passed here too, not just at corpus-build time above -
#   benchmark.py imports rag_pipeline, whose embed agent is instantiated from
#   these same env vars at import time, and every "rag"-strategy test case
#   calls embed() live on the question. Omitting these here was a real bug:
#   it silently fell back to the localhost:11434 default mid-run.
echo

# ---- 5. summary ------------------------------------------------------------------
echo "--- Summary ---"
"$PY" -c "
import json, collections
with open('$RESULTS_DIR/benchmark_results.json') as f:
    r = json.load(f)
print(f'Total runs in results file: {len(r)}')
print('Status breakdown:', dict(collections.Counter(x[\"status\"] for x in r)))
if r:
    print(f'Average score: {round(sum(x[\"score\"] for x in r) / len(r), 3)}')
print()
print('Full results: $RESULTS_DIR/benchmark_results.json and .csv')
"
