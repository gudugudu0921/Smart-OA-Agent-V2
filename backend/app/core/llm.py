"""Centralized OpenAI-compatible chat model initialization."""

from functools import lru_cache

from langchain_openai import ChatOpenAI

from app.core.config import settings


class LLMConfigurationError(RuntimeError):
    """Raised when the configured LLM cannot be initialized safely."""


class LLMUnavailableError(RuntimeError):
    """Raised when the configured upstream model cannot complete a request."""


def _is_missing(value: str, placeholders: set[str]) -> bool:
    normalized = value.strip()
    return not normalized or normalized in placeholders


@lru_cache
def get_llm() -> ChatOpenAI:
    """Return the single configured model instance without exposing secrets."""
    api_key = settings.llm_api_key.get_secret_value()
    missing = []
    if _is_missing(
        api_key,
        {"replace_with_llm_api_key", "replace_with_your_api_key"},
    ):
        missing.append("LLM_API_KEY")
    if _is_missing(settings.llm_base_url, set()):
        missing.append("LLM_BASE_URL")
    if _is_missing(settings.llm_model, {"your_model_name"}):
        missing.append("LLM_MODEL")
    if missing:
        raise LLMConfigurationError(
            "Missing LLM configuration: " + ", ".join(missing)
        )

    return ChatOpenAI(
        api_key=api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=0,
        extra_body={"thinking": {"type": "disabled"}},
    )
