"""Unsafe HTML in plain-text fields (text_safety) and the LLM shortening of too-long texts (shorten)."""
import json
import re

import pytest

from lo_pipeline.llm.service import LLMError
from lo_pipeline.questions import shorten
from lo_pipeline.questions.shorten import long_texts, shorten_long_texts
from lo_pipeline.questions.text_safety import clean_text, cut_at_word


# --- clean_text ------------------------------------------------------------------------------

@pytest.mark.parametrize("dirty, clean", [
    ("Plain text", "Plain text"),
    ("<b>bold</b> and <i>it</i>", "bold and it"),
    ("before<script>alert(1)</script>after", "beforeafter"),
    ("x <SCRIPT type='a'>evil()", "x"),  # unclosed script: the rest is dropped
    ("a<style>p{}</style>b", "ab"),
    ('<img src=x onerror="alert(1)">pic', "pic"),
    ('<x onclick="alert(1)">y</x>', "y</x>"),  # attributes -> removed; a bare unknown "</x>" is harmless
    ("<scr<b>ipt>alert(1)</script>", ""),  # removing <b> forms <script>: removed in the next round
    ("&lt;img src=x onerror=alert(1)&gt;ok", "ok"),  # decoded entities are cleaned too
    ("a <!-- hidden --> b", "a  b"),
    ("#include <stdio.h>", "#include <stdio.h>"),  # code stays
    ("vector<int> and Map<K, V>", "vector<int> and Map<K, V>"),
    ("if a < b and c > d", "if a < b and c > d"),
    ("Tom &amp; Jerry", "Tom & Jerry"),
])
def test_clean_text(dirty, clean):
    assert clean_text(dirty) == clean


def test_cut_at_word():
    assert cut_at_word("short", 10) == "short"
    assert cut_at_word("one two three four", 12) == "one two…"
    assert cut_at_word("x" * 30, 10) == "x" * 9 + "…"  # no space: hard cut
    assert len(cut_at_word("word " * 100, 255)) <= 255


# --- shorten_long_texts --------------------------------------------------------------------

def lo(**fields):
    base = {"generated_name": "Name", "question": "Question?", "answer": "Answer.",
            "mc_options": ["a", "b", "c", "d"], "hint": "Hint. [Source: c.pdf, p. 1]"}
    base.update(fields)
    return base


@pytest.fixture
def llm(monkeypatch):
    """Scripted LLM: shortens every item to its first 10 characters; `llm.reply` can replace that."""
    class Fake:
        prompts = []
        reply = None

        def __call__(self, messages, **kwargs):
            prompt = messages[-1]["content"]
            self.prompts.append(prompt)
            if self.reply is not None:
                answer = self.reply(prompt)
                if isinstance(answer, Exception):
                    raise answer
                return answer
            items = re.findall(r'^ITEM (\d+) \(at most \d+ characters\):\n"""(.*?)"""', prompt, flags=re.M | re.S)
            return json.dumps([{"item": int(n), "text": text[:10]} for n, text in items])
    fake = Fake()
    monkeypatch.setattr(shorten, "call_llm", fake)
    return fake


def test_nothing_too_long_makes_no_call(llm):
    assert shorten_long_texts([lo()], [{"name": "T", "description": "D", "hint": "H"}]) == {"long": 0, "shortened": 0}
    assert llm.prompts == []


def test_only_too_long_texts_are_sent_and_replaced(llm):
    los = [lo(generated_name="N" * 300), lo(question="Q" * 5000, mc_options=["a", "B" * 4500, "c", "d"]), lo()]
    tasks = [{"name": "T", "description": "D" * 4100, "hint": "H"}]
    stats = shorten_long_texts(los, tasks)
    assert stats == {"long": 4, "shortened": 4}
    assert los[0]["generated_name"] == "N" * 10
    assert los[1]["question"] == "Q" * 10 and los[1]["mc_options"] == ["a", "B" * 10, "c", "d"]
    assert tasks[0]["description"] == "D" * 10
    assert los[2] == lo()  # untouched
    assert len(llm.prompts) == 1  # 4 texts, batch size 5
    assert "at most 229 characters" in llm.prompts[0]  # 90 % of the 255 name limit


def test_hint_is_measured_as_escaped_html(llm):
    # 600 '<' fit as text (600 < 2000) but not as HTML ('&lt;' is 4 characters)
    found = long_texts([lo(hint="<" * 600 + " [Source: c.pdf, p. 1]")], [])
    assert [(f["key"], f["limit"]) for f in found] == [("hint", 1500)]
    los = [lo(hint="<" * 600 + " [Source: c.pdf, p. 1]")]
    shorten_long_texts(los, [])
    assert los[0]["hint"] == "<" * 10  # the "[Source: …]" suffix is gone; the source is its own hint


@pytest.mark.parametrize("answer", [
    "not json",
    json.dumps({"item": 1}),
    json.dumps([{"item": 1, "text": ""}]),  # empty
    json.dumps([{"item": 1, "text": "N" * 400}]),  # not shorter
    json.dumps([{"item": 7, "text": "x"}]),  # unknown item
    LLMError("down"),
])
def test_unusable_answer_keeps_the_text(llm, answer):
    llm.reply = lambda prompt: answer
    los = [lo(generated_name="N" * 300)]
    assert shorten_long_texts(los, []) == {"long": 1, "shortened": 0}
    assert los[0]["generated_name"] == "N" * 300


def test_other_errors_are_not_caught(llm):
    llm.reply = lambda prompt: RuntimeError("job timeout")  # must stop the job
    with pytest.raises(RuntimeError):
        shorten_long_texts([lo(generated_name="N" * 300)], [])


def test_shortened_name_loses_unsafe_html_but_question_text_stays_raw(llm):
    # names are plain text (cleaned); the question text is escaped later by units_output
    llm.reply = lambda prompt: json.dumps([{"item": 1, "text": "Short <script>alert(1)</script>"},
                                           {"item": 2, "text": "Why <stdio.h>?"}])
    los = [lo(generated_name="N" * 300, question="Q" * 5000)]
    shorten_long_texts(los, [])
    assert los[0]["generated_name"] == "Short"
    assert los[0]["question"] == "Why <stdio.h>?"


def test_question_text_is_measured_escaped(llm):
    # 1500 '<' fit as text but are 6000 characters escaped; the LLM gets the readable text
    found = long_texts([lo(question="<" * 1500)], [])
    assert [(f["key"], f["text"], f["as_html"]) for f in found] == [("question", "<" * 1500, True)]
    assert found[0]["limit"] == 900  # 90 % of 4000, scaled by 1500 / 6000 for the entities
