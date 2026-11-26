"""
Language of the documents and of the output (job option `language`: auto / sk / en).

detect_language counts frequent short words of Slovak and English and words with Slovak
diacritics in the extracted text; no LLM, no extra library. Czech text counts as Slovak (closest
supported language). With too little text to decide, the result is "en" with low confidence.

The output language is the requested one; `auto` (or nothing) means the language of the documents.
"""
import re
from typing import Iterable, Optional

SUPPORTED = ("sk", "en")

SK_WORDS = frozenset("""
a aj ako ale alebo ani až by bol bola boli bude budú do je ich iba jej je ju k ked keď ktorá ktoré
ktorý ktorí len lebo ma medzi môže na nad nie pod pre pri po s sa si so sú tak tiež to toho tom
tu už v vo z za ze že aby čo
""".split())

EN_WORDS = frozenset("""
a an and are as at be been but by can for from has have if in into is it its may not of on or
that the their there these this to was were which will with
""".split())

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_SK_LETTERS = set("áäčďéíĺľňóôŕšťúýž")
MIN_EVIDENCE = 20  # fewer recognised words than this: not enough to decide


def detect_language(texts: Iterable[str]) -> dict:
    """{"language": "sk" | "en", "confident": bool, "sk": score, "en": score}."""
    sk = en = 0
    for text in texts:
        for word in _WORD.findall(text.lower()):
            if word in SK_WORDS:
                sk += 1
            if word in EN_WORDS:
                en += 1
            if any(ch in _SK_LETTERS for ch in word):
                sk += 1
    language = "sk" if sk > en else "en"
    return {"language": language, "confident": max(sk, en) >= MIN_EVIDENCE and sk != en, "sk": sk, "en": en}


def source_language(language_state: dict) -> Optional[str]:
    """Language of what the student-facing prompts are built from: the atomic units (unit extraction
    may write them in English even for a Slovak document), else the documents' language."""
    return language_state.get("units") or language_state.get("document")


def output_language(requested: Optional[str], document: str) -> str:
    """The language questions are written in: the requested one, or the documents' for auto/None."""
    return requested if requested in SUPPORTED else document
