"""
Sentence embeddings (deduplication of units, checking a unit's page, graph candidates).

Provider from configuration (lo_pipeline/config.py), independent of the LLM provider:
  LOGEN_EMBED_PROVIDER=local              all-MiniLM-L6-v2 in this process (default, in the Docker image)
  LOGEN_EMBED_PROVIDER=openai_compatible  /v1/embeddings of an API (OpenAI, a local vLLM or Ollama server):
                                          LOGEN_EMBED_MODEL, LOGEN_EMBED_BASE_URL, LOGEN_EMBED_API_KEY
                                          (without base URL / key, those of the LLM provider are used)
Note: the similarity thresholds (LOGEN_DEDUP_THRESHOLD and others) are calibrated for MiniLM;
another embedding model needs them checked again.
"""
import threading
from typing import List

import numpy as np
import logging

from lo_pipeline.config import get_settings

logger = logging.getLogger(__name__)

LOCAL_DEFAULT_MODEL = "all-MiniLM-L6-v2"
API_BATCH = 256  # texts per /v1/embeddings request

# Loaded once, slow to load, fast to use
_model = None
_api_client = None
# LLM batches run in parallel threads (llm/parallel.py): the model is loaded once and used by one
# thread at a time (encoding is fast and torch already uses several cores for it)
_lock = threading.RLock()


def get_model():
    """The local SentenceTransformer model (imported only when used: no torch needed with an API)."""
    global _model
    with _lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            name = get_settings().embed_model
            logger.info(f"[Embeddings] Loading local model {name}...")
            _model = SentenceTransformer(name)
            logger.info("[Embeddings] Model loaded.")
        return _model


def _api():
    """OpenAI-compatible embeddings client: LOGEN_EMBED_BASE_URL / _API_KEY, else the LLM provider's."""
    global _api_client
    from lo_pipeline.llm.service import LLMConfigError
    with _lock:
        if _api_client is None:
            from openai import OpenAI
            settings = get_settings()
            if settings.embed_model == LOCAL_DEFAULT_MODEL:
                raise LLMConfigError("LOGEN_EMBED_PROVIDER=openai_compatible needs LOGEN_EMBED_MODEL "
                                     "(the name of the embedding model at the API).")
            provider = settings.provider()
            base_url = settings.embed_base_url or (provider.base_url if provider else None)
            key = settings.embed_api_key.get_secret_value() if settings.embed_api_key else None
            key = key or (provider.api_key if provider else None) or "not-needed"
            logger.info(f"[Embeddings] API {base_url or 'api.openai.com'}, model {settings.embed_model}")
            _api_client = OpenAI(api_key=key, base_url=base_url) if base_url else OpenAI(api_key=key)
        return _api_client


def _embed_api(texts: List[str]) -> np.ndarray:
    import openai
    from lo_pipeline.llm.service import LLMConfigError, LLMError

    model = get_settings().embed_model
    vectors = []
    for start in range(0, len(texts), API_BATCH):
        try:
            response = _api().embeddings.create(model=model, input=texts[start:start + API_BATCH])
        except (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError) as e:
            raise LLMConfigError(f"Embedding API refused the request (HTTP {e.status_code}): "
                                 f"check LOGEN_EMBED_MODEL / LOGEN_EMBED_API_KEY.") from e
        except openai.OpenAIError as e:  # the client already retried temporary errors
            raise LLMError(f"Embedding API failed: {e}") from e
        vectors.extend(item.embedding for item in sorted(response.data, key=lambda item: item.index))
    return np.array(vectors, dtype=np.float32)


def get_embeddings_batch(texts: List[str]) -> List[np.ndarray]:
    """
    One vector per text, in order. More efficient than calling get_embedding in a loop.
    """
    texts = list(texts)
    if get_settings().embed_provider == "openai_compatible":
        return _embed_api(texts) if texts else np.zeros((0, 0), dtype=np.float32)
    with _lock:
        return get_model().encode(texts, convert_to_numpy=True)


def get_embedding(text: str) -> np.ndarray:
    return get_embeddings_batch([text])[0]


def cosine_similarity(vec1: np.ndarray, vec2: np.ndarray) -> float:
    """
    Returns the cosine similarity between two vectors.
    Range: -1 to 1, but for sentence embeddings typically 0 to 1
    """
    norm1 = np.linalg.norm(vec1)
    norm2 = np.linalg.norm(vec2)
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return float(np.dot(vec1, vec2) / (norm1 * norm2))


def semantic_similarity(text_a: str, text_b: str) -> float:
    """
    Convenience function - embeds two texts and returns their similarity.
    For bulk operations use get_embeddings_batch instead.
    """
    return cosine_similarity(*get_embeddings_batch([text_a, text_b]))
