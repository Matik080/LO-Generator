import re


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
