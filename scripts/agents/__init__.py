"""
Pluggable model-serving adapters for the benchmark harness.

The point of this package: `benchmark.py` (SQL generation) and `rag_pipeline.py`
(embeddings) should not care whether the model they're calling lives behind
Ollama's native API, vLLM's OpenAI-compatible server, or a hosted external API
(OpenAI, Together, Groq, etc.) - they just call `agent.generate(model, prompt)`
or `agent.embed(text, model)` and get back a normalized dict.

Every adapter class exposes:
    generate(model: str, prompt: str) -> dict
        {
          "raw_text": str,
          "wall_time_s": float,
          "prompt_tokens": int | None,
          "completion_tokens": int | None,
          "tokens_per_sec": float | None,
          "provider": str,
          "raw_metrics": dict,   # provider-specific extras, kept for debugging
        }

    embed(text: str, model: str) -> list[float]
        Only implemented where the backend actually serves embeddings. vLLM's
        chat-completions server (e.g. multi-user-vllm) normally does NOT serve
        embeddings unless a separate embedding model/endpoint is deployed - see
        docs/AGENTS.md. In practice you'll usually still point the *embedding*
        step at an Ollama instance running nomic-embed-text even when the agent
        under test is vLLM or an external API; that's why embeddings are
        configured independently (EMBED_AGENT_TYPE/EMBED_URL/...) from the
        agent under test (AGENT_TYPE/AGENT_URL/...) everywhere in this repo.

Supported agent_type values:
    "ollama"                          - Ollama's native /api/generate + /api/embeddings
    "openai" / "openai_compat" / "vllm" - any OpenAI-compatible /v1/chat/completions
                                          server: vLLM's built-in OpenAI server
                                          (e.g. multi-user-vllm on :8000), LM Studio,
                                          Together, Groq, or the real OpenAI API.
"""
from .ollama_agent import OllamaAgent
from .openai_agent import OpenAICompatAgent

DEFAULT_URLS = {
    "ollama": "http://localhost:11434",
    "openai": "http://localhost:8000/v1",
}


def get_agent(agent_type, base_url=None, api_key=None, timeout=300):
    """Factory: returns an agent instance for the given agent_type.

    base_url: if None, falls back to a sensible per-type default (localhost
    Ollama or localhost vLLM). Always pass it explicitly for anything remote
    or external.
    """
    agent_type = (agent_type or "ollama").strip().lower()

    if agent_type == "ollama":
        return OllamaAgent(base_url=base_url or DEFAULT_URLS["ollama"], timeout=timeout)

    if agent_type in ("openai", "openai_compat", "vllm"):
        return OpenAICompatAgent(
            base_url=base_url or DEFAULT_URLS["openai"], api_key=api_key, timeout=timeout
        )

    raise ValueError(
        f"Unknown agent_type '{agent_type}' - expected 'ollama' or 'openai' "
        f"(aliases: 'openai_compat', 'vllm')"
    )
