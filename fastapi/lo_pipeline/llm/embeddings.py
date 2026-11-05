from typing import List
import numpy as np
from sentence_transformers import SentenceTransformer

# Loading once at module level, slow to load, fast to use
_model = None

def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        print("[Embeddings] Loading model...")
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        print("[Embeddings] Model loaded.")
    return _model

def get_embedding(text: str) -> np.ndarray:
    model = get_model()
    return model.encode(text, convert_to_numpy=True)

def get_embeddings_batch(texts: List[str]) -> List[np.ndarray]:
    """
    More efficient than calling get_embedding in a loop.
    Use this when you have multiple texts to embed at once.
    """
    model = get_model()
    return model.encode(texts, convert_to_numpy=True)

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
    vec_a = get_embedding(text_a)
    vec_b = get_embedding(text_b)
    return cosine_similarity(vec_a, vec_b)