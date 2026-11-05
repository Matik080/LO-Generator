import spacy

import os
SPACY_MODEL = os.environ.get("SPACY_MODEL", "en_core_web_sm")
nlp = spacy.load(SPACY_MODEL)

def normalize_concept(concept: str):
    doc = nlp(concept.lower())
    lemmas = [token.lemma_ for token in doc if not token.is_stop]
    return " ".join(lemmas)

def normalize_units(units):
    for unit in units:
        normalized = []
        for concept in unit.get("concepts", []):
            normalized.append(normalize_concept(concept))
        unit["normalized_concepts"] = list(set(normalized))
    return units