from lo_pipeline.files import export_json
from lo_pipeline.lo.lexical import tokenize, lexical_overlap  # noqa: F401  (re-exported for older scripts)
from lo_pipeline.llm.embeddings import get_embeddings_batch, cosine_similarity

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