"""Embeddings (lo_pipeline/llm/embeddings.py): local MiniLM by default, or an OpenAI-compatible API."""

from types import SimpleNamespace

import httpx
import numpy as np
import openai
import pytest

from lo_pipeline.config import PipelineSettings
from lo_pipeline.llm import embeddings
from lo_pipeline.llm.service import LLMConfigError, LLMError


@pytest.fixture
def settings(monkeypatch):
    def use(**values):
        s = PipelineSettings(_env_file=None, **values)
        monkeypatch.setattr(embeddings, "get_settings", lambda: s)
        monkeypatch.setattr(embeddings, "_api_client", None)
        return s
    return use


class FakeEmbeddingsAPI:
    """Returns [len(text), index] per text, in reverse order (the API may reorder; `index` tells)."""

    def __init__(self, error=None):
        self.requests, self.error = [], error
        self.embeddings = SimpleNamespace(create=self.create)

    def create(self, model, input):
        self.requests.append((model, list(input)))
        if self.error:
            raise self.error
        data = [SimpleNamespace(index=i, embedding=[float(len(t)), float(i)]) for i, t in enumerate(input)]
        return SimpleNamespace(data=list(reversed(data)))


def test_local_minilm_by_default(settings):
    settings()
    vectors = embeddings.get_embeddings_batch(["A pointer stores an address.", "Pointers hold addresses."])
    assert len(vectors) == 2 and vectors[0].shape == (384,)
    assert embeddings.cosine_similarity(vectors[0], vectors[1]) > 0.5
    assert np.allclose(embeddings.get_embedding("A pointer stores an address."), vectors[0], atol=1e-5)


def test_api_embeddings_in_order_and_in_chunks(settings, monkeypatch):
    settings(embed_provider="openai_compatible", embed_model="text-embedding-3-small")
    api = FakeEmbeddingsAPI()
    monkeypatch.setattr(embeddings, "_api", lambda: api)
    monkeypatch.setattr(embeddings, "API_BATCH", 2)
    vectors = embeddings.get_embeddings_batch(["a", "bb", "ccc"])
    assert vectors.tolist() == [[1.0, 0.0], [2.0, 1.0], [3.0, 0.0]]  # order restored, 2 requests
    assert api.requests == [("text-embedding-3-small", ["a", "bb"]), ("text-embedding-3-small", ["ccc"])]


def test_api_needs_an_embedding_model(settings):
    settings(embed_provider="openai_compatible", groq_api_key="gsk")
    with pytest.raises(LLMConfigError, match="LOGEN_EMBED_MODEL"):
        embeddings.get_embeddings_batch(["a"])


def test_api_uses_llm_provider_unless_set(settings):
    settings(embed_provider="openai_compatible", embed_model="nomic-embed-text",
             llm_base_url="http://ollama:11434/v1")
    assert str(embeddings._api().base_url).startswith("http://ollama:11434/v1")
    settings(embed_provider="openai_compatible", embed_model="nomic-embed-text",
             llm_base_url="http://ollama:11434/v1", embed_base_url="http://embedder:8080/v1")
    assert str(embeddings._api().base_url).startswith("http://embedder:8080/v1")


@pytest.mark.parametrize("error, expected", [
    (openai.AuthenticationError("401", response=httpx.Response(401, request=httpx.Request("POST", "http://x")),
                                body=None), LLMConfigError),
    (openai.APIConnectionError(request=httpx.Request("POST", "http://x")), LLMError),
])
def test_api_errors(settings, monkeypatch, error, expected):
    settings(embed_provider="openai_compatible", embed_model="m")
    monkeypatch.setattr(embeddings, "_api", lambda: FakeEmbeddingsAPI(error))
    with pytest.raises(expected):
        embeddings.get_embeddings_batch(["a"])
