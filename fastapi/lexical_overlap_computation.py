import re
from utilities import export_json
from embedding_service import get_embeddings_batch, cosine_similarity

def compute_semantic_similarities(learning_objects):
    """
    Computes embedding-based similarity between each learning object's answer and its source statement. Adds 'semantic_similarity' field.
    """
    statements = [lo["source_statement"] for lo in learning_objects]
    answers = [lo["answer"] for lo in learning_objects]

    print("[Semantic] Computing embeddings for source statements...")
    statement_embeddings = get_embeddings_batch(statements)
    print("[Semantic] Computing embeddings for answers...")
    answer_embeddings = get_embeddings_batch(answers)

    similarities = []
    for lo, s_emb, a_emb in zip(learning_objects,
                                statement_embeddings,
                                answer_embeddings):
        sim = cosine_similarity(s_emb, a_emb)
        lo["semantic_similarity"] = round(sim, 4)
        similarities.append(sim)

    return similarities

def tokenize(text):
    text = text.lower()
    tokens = re.findall(r"\b\w+\b", text)
    return tokens

def lexical_overlap(source, answer):
    source_tokens = set(tokenize(source))
    answer_tokens = set(tokenize(answer))
    if not answer_tokens:
        return 0.0

    overlap = source_tokens.intersection(answer_tokens)
    return len(overlap) / len(answer_tokens)

def compute_overlaps(learning_objects):
    overlaps = []

    for lo in learning_objects:
        source = lo["source_statement"]
        answer = lo['answer']

        score = lexical_overlap(source, answer)
        lo["lexical_overlap"] = score
        overlaps.append(score)

    export_json(learning_objects, "./output/learning_objects.json")
    return overlaps