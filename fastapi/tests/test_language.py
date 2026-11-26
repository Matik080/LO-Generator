"""Document language detection and the output language (job option `language`)."""
import pytest

from lo_pipeline.language import detect_language, output_language

EN = ("A pointer is a variable that stores the address of another variable. The value of a pointer can be "
      "changed, and it is used to access memory that was allocated on the heap. If the allocation fails, "
      "malloc returns a null pointer, which must be checked before it is used in the program.")
SK = ("Smerník je premenná, ktorá uchováva adresu inej premennej. Hodnotu smerníka je možné zmeniť a používa "
      "sa na prístup k pamäti, ktorá bola pridelená na halde. Ak pridelenie zlyhá, funkcia malloc vráti "
      "nulový smerník, ktorý treba skontrolovať skôr, ako sa v programe použije.")


def test_english_and_slovak_text():
    en, sk = detect_language([EN]), detect_language([SK])
    assert en["language"] == "en" and en["confident"]
    assert sk["language"] == "sk" and sk["confident"]


def test_several_documents_count_together():
    assert detect_language([SK, SK, EN])["language"] == "sk"
    assert detect_language([EN, EN, SK])["language"] == "en"


def test_too_little_text_is_not_confident():
    result = detect_language(["Malloc", "x = 1"])
    assert result["language"] == "en" and not result["confident"]
    assert detect_language([])["language"] == "en"


@pytest.mark.parametrize("requested, document, expected", [
    ("auto", "sk", "sk"), (None, "en", "en"), ("sk", "en", "sk"), ("en", "sk", "en"), ("de", "en", "en"),
])
def test_output_language(requested, document, expected):
    assert output_language(requested, document) == expected
