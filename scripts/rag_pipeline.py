"""
RAG pipeline: builds a corpus of table-schema chunks + gold NL->SQL example chunks,
embeds them, and provides a retrieval function (cosine similarity) to build
RAG-augmented prompts.

No vector DB — plain in-memory list + math, per spec (small corpus, benchmarking
exercise).

Embeddings are pluggable (see scripts/agents/) and configured INDEPENDENTLY from
whichever agent benchmark.py is testing (AGENT_TYPE/AGENT_URL). This matters
because the agent under test is very often NOT an embedding server - e.g. vLLM's
chat-completions server (multi-user-vllm) or an external OpenAI-style API
typically don't serve embeddings unless a dedicated embedding model/endpoint is
deployed alongside them. In practice you'll usually still point EMBED_* at an
Ollama instance running nomic-embed-text even when benchmarking vLLM or an
external agent. See docs/AGENTS.md for a full walkthrough.
"""
import json
import math
import os
import sys

# EMBED_AGENT_TYPE/EMBED_URL/EMBED_MODEL/EMBED_API_KEY: the embedding backend,
# independent of the agent under test in benchmark.py (AGENT_TYPE/AGENT_URL).
# Defaults preserve this project's original behavior (Ollama + nomic-embed-text
# on localhost) so existing setups keep working unchanged.
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)
from agents import get_agent

EMBED_AGENT_TYPE = os.environ.get("EMBED_AGENT_TYPE", "ollama")
EMBED_URL = os.environ.get("EMBED_URL", os.environ.get("OLLAMA_URL"))
EMBED_API_KEY = os.environ.get("EMBED_API_KEY")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
_embed_agent = get_agent(EMBED_AGENT_TYPE, base_url=EMBED_URL, api_key=EMBED_API_KEY)

TABLE_DOCS = {
    "REGIONS": "Sales schema table REGIONS(REGION_ID NUMBER PK, REGION_NAME VARCHAR2, COUNTRY VARCHAR2). "
               "Lookup table of sales regions and their countries.",
    "CUSTOMERS": "Sales schema table CUSTOMERS(CUSTOMER_ID NUMBER PK, FIRST_NAME VARCHAR2, LAST_NAME VARCHAR2, "
                 "EMAIL VARCHAR2, REGION_ID NUMBER FK->REGIONS, SIGNUP_DATE DATE, CUSTOMER_TIER VARCHAR2 "
                 "['STANDARD','SILVER','GOLD','PLATINUM']). Customer master data.",
    "PRODUCTS": "Sales schema table PRODUCTS(PRODUCT_ID NUMBER PK, PRODUCT_NAME VARCHAR2, CATEGORY VARCHAR2, "
                "UNIT_PRICE NUMBER, STOCK_QTY NUMBER). Product catalog with pricing and inventory.",
    "ORDERS": "Sales schema table ORDERS(ORDER_ID NUMBER PK, CUSTOMER_ID NUMBER FK->CUSTOMERS, ORDER_DATE DATE, "
              "STATUS VARCHAR2 ['PENDING','COMPLETED','CANCELLED','REFUNDED'], REGION_ID NUMBER FK->REGIONS). "
              "Order header records.",
    "ORDER_ITEMS": "Sales schema table ORDER_ITEMS(ORDER_ITEM_ID NUMBER PK, ORDER_ID NUMBER FK->ORDERS, "
                   "PRODUCT_ID NUMBER FK->PRODUCTS, QUANTITY NUMBER, UNIT_PRICE NUMBER). Line items per order; "
                   "used for revenue calculations (QUANTITY * UNIT_PRICE).",
    "HR_LOCATIONS": "HR schema table HR_LOCATIONS(LOCATION_ID NUMBER PK, CITY VARCHAR2, COUNTRY VARCHAR2). "
                    "Physical office locations.",
    "HR_DEPARTMENTS": "HR schema table HR_DEPARTMENTS(DEPARTMENT_ID NUMBER PK, DEPARTMENT_NAME VARCHAR2, "
                      "LOCATION_ID NUMBER FK->HR_LOCATIONS). Company departments.",
    "HR_JOBS": "HR schema table HR_JOBS(JOB_ID NUMBER PK, JOB_TITLE VARCHAR2, MIN_SALARY NUMBER, MAX_SALARY NUMBER). "
               "Job title catalog with salary bands.",
    "HR_EMPLOYEES": "HR schema table HR_EMPLOYEES(EMPLOYEE_ID NUMBER PK, FIRST_NAME VARCHAR2, LAST_NAME VARCHAR2, "
                    "EMAIL VARCHAR2, HIRE_DATE DATE, JOB_ID NUMBER FK->HR_JOBS, SALARY NUMBER, "
                    "DEPARTMENT_ID NUMBER FK->HR_DEPARTMENTS, MANAGER_ID NUMBER FK->HR_EMPLOYEES self-referencing). "
                    "Employee master data with self-referencing manager hierarchy.",
    "HR_JOB_HISTORY": "HR schema table HR_JOB_HISTORY(EMPLOYEE_ID NUMBER FK->HR_EMPLOYEES, START_DATE DATE, "
                      "END_DATE DATE nullable, JOB_ID NUMBER FK->HR_JOBS, DEPARTMENT_ID NUMBER FK->HR_DEPARTMENTS, "
                      "PK(EMPLOYEE_ID, START_DATE)). Historical record of past roles/departments per employee.",
}

TABLES_BY_SCHEMA = {
    "sales": ["REGIONS", "CUSTOMERS", "PRODUCTS", "ORDERS", "ORDER_ITEMS"],
    "hr": ["HR_LOCATIONS", "HR_DEPARTMENTS", "HR_JOBS", "HR_EMPLOYEES", "HR_JOB_HISTORY"],
}


def embed(text):
    """Delegates to whichever embedding agent EMBED_AGENT_TYPE resolved to."""
    return _embed_agent.embed(text, model=EMBED_MODEL)


def cosine_sim(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def build_corpus(test_cases):
    """Build table chunks + one example chunk per gold test case."""
    chunks = []
    for tname, desc in TABLE_DOCS.items():
        chunks.append({"chunk_id": f"table::{tname}", "type": "table", "table_name": tname, "text": desc})
    for tc in test_cases:
        example_text = f"Example question: {tc['prompt']}\nSQL: {tc['gold_sql']}"
        chunks.append({
            "chunk_id": f"example::{tc['id']}", "type": "example", "test_case_id": tc["id"],
            "schema": tc["schema"], "tier": tc["tier"], "text": example_text,
        })
    return chunks


def embed_corpus(chunks):
    for i, c in enumerate(chunks):
        c["embedding"] = embed(c["text"])
        if (i + 1) % 10 == 0:
            print(f"  embedded {i + 1}/{len(chunks)}")
    return chunks


def retrieve(question, corpus, k_tables=3, k_examples=2, exclude_test_case_id=None):
    q_emb = embed(question)

    table_chunks = [c for c in corpus if c["type"] == "table"]
    example_chunks = [c for c in corpus if c["type"] == "example"
                       and c.get("test_case_id") != exclude_test_case_id]

    table_scored = sorted(
        ((cosine_sim(q_emb, c["embedding"]), c) for c in table_chunks),
        key=lambda x: -x[0])
    example_scored = sorted(
        ((cosine_sim(q_emb, c["embedding"]), c) for c in example_chunks),
        key=lambda x: -x[0])

    top_tables = [c for score, c in table_scored[:k_tables]]
    top_examples = [c for score, c in example_scored[:k_examples]]
    return top_tables, top_examples


def build_rag_prompt(question, top_tables, top_examples):
    schema_block = "\n".join(f"- {TABLE_DOCS[t['table_name']]}" for t in top_tables)
    examples_block = "\n\n".join(e["text"] for e in top_examples)
    prompt = f"""You are an expert Oracle SQL developer. Given the schema and examples below, write a single Oracle SQL query that answers the question. Output ONLY the SQL query, no explanation, no markdown code fences.

Relevant schema:
{schema_block}

Similar examples:
{examples_block}

Question: {question}

SQL:"""
    return prompt


def build_static_prompt(question, schema):
    all_tables = TABLES_BY_SCHEMA[schema]
    schema_block = "\n".join(f"- {TABLE_DOCS[t]}" for t in all_tables)
    prompt = f"""You are an expert Oracle SQL developer. Given the full schema below, write a single Oracle SQL query that answers the question. Output ONLY the SQL query, no explanation, no markdown code fences.

Full schema:
{schema_block}

Question: {question}

SQL:"""
    return prompt


if __name__ == "__main__":
    # NOTE: previously hardcoded to /root/test_cases.json (only worked when run
    # from the original remote box's layout). Now uses TEST_CASES_PATH like every
    # other script in this branch, defaulting to ../testcases/test_cases.json
    # relative to this file.
    test_cases_path = os.environ.get(
        "TEST_CASES_PATH", os.path.join(SCRIPTS_DIR, "..", "testcases", "test_cases.json")
    )
    with open(test_cases_path) as f:
        test_cases = json.load(f)

    print(f"Building corpus from {len(test_cases)} test cases + {len(TABLE_DOCS)} tables...")
    print(f"Embedding via: type={EMBED_AGENT_TYPE} url={_embed_agent.base_url} model={EMBED_MODEL}")
    corpus = build_corpus(test_cases)
    print(f"Corpus size: {len(corpus)} chunks. Embedding...")
    corpus = embed_corpus(corpus)

    rag_corpus_path = os.environ.get(
        "RAG_CORPUS_PATH", os.path.join(SCRIPTS_DIR, "..", "testcases", "rag_corpus.json")
    )
    with open(rag_corpus_path, "w") as f:
        json.dump(corpus, f)
    print(f"Saved corpus with embeddings to {rag_corpus_path}")

    # Sanity check: for a handful of test cases, verify retrieval surfaces the tables
    # actually referenced in their gold SQL.
    print("\n=== Retrieval sanity check ===")
    sample_ids = [tc["id"] for tc in test_cases if tc["tier"] in (2, 3)][:10]
    hits, total = 0, 0
    for tc in test_cases:
        if tc["id"] not in sample_ids:
            continue
        top_tables, top_examples = retrieve(tc["prompt"], corpus, k_tables=3, k_examples=2,
                                             exclude_test_case_id=tc["id"])
        retrieved_names = {t["table_name"] for t in top_tables}
        referenced = {tn for tn in TABLE_DOCS if tn in tc["gold_sql"].upper()}
        covered = referenced.issubset(retrieved_names)
        total += 1
        hits += int(covered)
        status = "OK" if covered else "MISS"
        print(f"{status:4s} {tc['id']:12s} referenced={sorted(referenced)} retrieved={sorted(retrieved_names)}")
    print(f"\nRetrieval coverage: {hits}/{total} test cases had all referenced tables retrieved (k_tables=3)")
