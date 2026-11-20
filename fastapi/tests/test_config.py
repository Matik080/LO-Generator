"""Configuration (lo_pipeline/config.py): LOGEN_* variables, older names as fallback, defaults."""

import pytest
from pydantic import ValidationError

from lo_pipeline.config import DEFAULT_EVAL_MODELS, GROQ_BASE_URL, OPENROUTER_BASE_URL, PipelineSettings
from lo_pipeline.runner import PipelineOptions

ALL_NAMES = [
    "LOGEN_LLM_BASE_URL", "LOGEN_LLM_API_KEY", "LOGEN_LLM_MODEL", "GEN_MODEL", "LOGEN_LLM_MODEL_EVAL",
    "LOGEN_INTER_CALL_DELAY", "INTER_CALL_DELAY", "LOGEN_REASONING_EFFORT", "REASONING_EFFORT",
    "GROQ_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN", "OPENAI_API_KEY", "LOGEN_MAX_ATTEMPTS",
    "LOGEN_EVAL_BATCH_SIZE", "LOGEN_EMBED_PROVIDER", "LOGEN_LLM_MAX_CONCURRENCY",
]


@pytest.fixture
def env(monkeypatch):
    """A clean environment (no fastapi/.env, none of the variables set); returns a setter."""
    for name in ALL_NAMES:
        monkeypatch.delenv(name, raising=False)

    def settings(**variables) -> PipelineSettings:
        for name in ALL_NAMES:  # each call starts clean: only the given variables are set
            monkeypatch.delenv(name, raising=False)
        for name, value in variables.items():
            monkeypatch.setenv(name, value)
        return PipelineSettings(_env_file=None)
    return settings


def test_defaults_are_the_old_built_in_values(env):
    s = env()
    assert s.provider() is None and s.default_model() is None
    assert s.eval_models == DEFAULT_EVAL_MODELS
    assert s.llm_max_concurrency == 4 and s.inter_call_delay == 0.0
    assert s.embed_provider == "local" and s.embed_model == "all-MiniLM-L6-v2"
    assert PipelineOptions.from_settings(s) == PipelineOptions()  # same as the hard-coded defaults


def test_old_variable_names_still_work(env):
    s = env(GROQ_API_KEY="gsk-1", GEN_MODEL="qwen/qwen3.8-27b", INTER_CALL_DELAY="0.5", REASONING_EFFORT="low")
    provider = s.provider()
    assert (provider.name, provider.api_key, provider.base_url) == ("Groq", "gsk-1", GROQ_BASE_URL)
    assert s.default_model() == "qwen/qwen3.8-27b"
    assert s.inter_call_delay == 0.5 and s.reasoning_effort == "low"


def test_logen_names_win_over_old_ones(env):
    s = env(GEN_MODEL="old-model", LOGEN_LLM_MODEL="new-model", INTER_CALL_DELAY="0.5", LOGEN_INTER_CALL_DELAY="1")
    assert s.llm_model == "new-model" and s.inter_call_delay == 1.0


def test_old_provider_order(env):
    assert env(OPENROUTER_API_KEY="or", GITHUB_TOKEN="gh").provider().base_url == OPENROUTER_BASE_URL
    assert env(GROQ_API_KEY="gsk").default_model() == "openai/gpt-oss-120b"
    s = env(GROQ_API_KEY="gsk", OPENROUTER_API_KEY="or")  # with OpenRouter too, the thesis model is the default
    assert s.provider().name == "Groq" and s.default_model() == "llama-3.3-70b-versatile"


def test_any_openai_compatible_provider(env):
    s = env(LOGEN_LLM_BASE_URL="http://gpu-server:8001/v1", LOGEN_LLM_MODEL="qwen-27b", GROQ_API_KEY="gsk")
    provider = s.provider()
    assert (provider.name, provider.base_url) == ("gpu-server", "http://gpu-server:8001/v1")
    assert provider.api_key == "not-needed"  # a local server without a key
    assert s.default_model() == "qwen-27b"
    assert env(LOGEN_LLM_API_KEY="sk-x").provider().base_url is None  # key only: OpenAI's own API


def test_keys_are_not_shown(env):
    s = env(GROQ_API_KEY="gsk-secret")
    assert "gsk-secret" not in repr(s) and "gsk-secret" not in str(s.model_dump())


def test_pipeline_settings_reach_the_options(env):
    s = env(LOGEN_LLM_MODEL_EVAL="model-a, model-b", LOGEN_MAX_ATTEMPTS="1")
    options = PipelineOptions.from_settings(s)
    assert options.eval_models == ["model-a", "model-b"] and options.max_attempts == 1


def test_eval_batch_size_override(env, monkeypatch):
    from lo_pipeline.config import get_settings
    from lo_pipeline.lo.evaluator import eval_batch_size

    assert eval_batch_size("openai/gpt-oss-120b") == 2 and eval_batch_size("other") == 8
    monkeypatch.setenv("LOGEN_EVAL_BATCH_SIZE", "4")
    get_settings.cache_clear()
    assert eval_batch_size("openai/gpt-oss-120b") == 4


def test_example_env_file_is_valid(monkeypatch, tmp_path):
    """.env.example as it is, and with every commented setting switched on, gives valid settings."""
    from pathlib import Path
    from service.settings import ServiceSettings

    for name in ALL_NAMES:
        monkeypatch.delenv(name, raising=False)
    example = Path(__file__).resolve().parents[1] / ".env.example"
    assert PipelineSettings(_env_file=example).default_model() == "qwen/qwen3.8-27b"
    switched_on = tmp_path / ".env"
    switched_on.write_text("\n".join(line[2:] if line.startswith("# LOGEN_") else line
                                     for line in example.read_text(encoding="utf-8").splitlines()), encoding="utf-8")
    s = PipelineSettings(_env_file=switched_on)
    assert PipelineOptions.from_settings(s) == PipelineOptions(eval_models=list(DEFAULT_EVAL_MODELS))
    assert s.eval_batch_size is None and s.provider().base_url == GROQ_BASE_URL
    assert ServiceSettings(_env_file=switched_on).max_active_jobs == 10


@pytest.mark.parametrize("variables", [
    {"LOGEN_LLM_MODEL_EVAL": " , "}, {"LOGEN_EMBED_PROVIDER": "cloud"}, {"LOGEN_LLM_MAX_CONCURRENCY": "0"},
])
def test_wrong_settings_are_errors(env, variables):
    with pytest.raises(ValidationError):
        env(**variables)
