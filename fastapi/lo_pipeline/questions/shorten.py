"""
Texts longer than their ECEH column are shortened by the LLM once (design kap. 6, point 2).

Runs at the end of the `questions` stage, so the checkpoint keeps the answers. It looks at every text
that becomes part of a question: LO name, question, answer, MC options and hint, and the name,
text and hint of no_answer tasks. Each text is measured as the output will write it (a name as plain
text, question text and choices escaped, a hint as '<p>…</p>'). Only texts over the limit are sent,
several per call; a
shorter non-empty answer replaces the text. What is still too long is handled by units_output:
a name is cut at the end of a word, a hint is left out, a question with a too-long text or choice
is left out (warning field_too_long).

Normally nothing is over the limits (255 characters for names, 4000 for texts), so no call is made.
An LLM error or an invalid answer keeps the texts as they are (the output then handles them).
"""
import html
import logging
from typing import Any, Optional

from lo_pipeline.contract.units import CHOICE_TEXT_MAX, HINT_URL_MAX, QUESTION_DESCRIPTION_MAX, QUESTION_NAME_MAX
from lo_pipeline.llm.json_load import parse_json
from lo_pipeline.llm.service import LLMError, call_llm
from lo_pipeline.questions.text_safety import clean_text
from lo_pipeline.questions.units_output import hint_fits, html_text, llm_hint_text

logger = logging.getLogger(__name__)

TARGET_SHARE = 0.9  # ask for 90 % of the limit, so a slightly long answer still fits
HINT_TEXT_TARGET = 1500  # characters of hint text (escaping and '<p></p>' need room up to HINT_URL_MAX)


def _plain_text(value) -> str:
    return clean_text(value.strip()) if isinstance(value, str) else ""


def _raw_text(value) -> str:
    return html.unescape(value.strip()) if isinstance(value, str) else ""


def long_texts(los: list[dict], tasks: list[dict]) -> list[dict]:
    """Texts over their limit: [{"obj", "key", "index" (MC option) or None, "text", "limit", "as_html"}].

    Each is measured as units_output writes it: names as plain text, question text and choices
    escaped (ECEH shows them as HTML, so "<" counts as "&lt;"), hints as '<p>…</p>'. `text` is what
    the LLM gets (readable, not escaped).
    """
    found = []

    def check(obj: dict, key: str, limit: int, index: Optional[int] = None, as_html: bool = False):
        value = obj[key][index] if index is not None else obj.get(key)
        length = len(html_text(value)) if as_html else len(_plain_text(value))
        if length > limit:
            text = _raw_text(value) if as_html else _plain_text(value)
            # an escaped text needs room for its entities: ask for less in proportion
            target = int(limit * TARGET_SHARE * len(text) / length)
            found.append({"obj": obj, "key": key, "index": index, "text": text, "limit": target, "as_html": as_html})

    def check_hint(obj: dict, hint: str):
        if hint and not hint_fits(hint, HINT_URL_MAX):
            found.append({"obj": obj, "key": "hint", "index": None, "text": hint, "limit": HINT_TEXT_TARGET,
                          "as_html": True})

    for lo in los:
        check(lo, "generated_name", QUESTION_NAME_MAX)
        check(lo, "question", QUESTION_DESCRIPTION_MAX, as_html=True)
        check(lo, "answer", CHOICE_TEXT_MAX, as_html=True)
        if isinstance(lo.get("mc_options"), list):
            for k in range(len(lo["mc_options"])):
                check(lo, "mc_options", CHOICE_TEXT_MAX, k, as_html=True)
        check_hint(lo, llm_hint_text(lo))
    for task in tasks:
        check(task, "name", QUESTION_NAME_MAX)
        check(task, "description", QUESTION_DESCRIPTION_MAX, as_html=True)
        check_hint(task, _raw_text(task.get("hint")))
    return found


def _prompt(batch: list[dict]) -> list[dict]:
    items = "\n\n".join(f'ITEM {n} (at most {item["limit"]} characters):\n"""{item["text"]}"""'
                        for n, item in enumerate(batch, start=1))
    prompt = f"""
Shorten each text below to at most the given number of characters.
- Keep the meaning, all facts needed to answer or understand it, and the language of the text.
- Keep code, formulas and names exactly as they are; remove only words that are not needed.
- Do not add anything new.

{items}

Return a JSON array with exactly {len(batch)} results in the same order:
[
  {{"item": 1, "text": "..."}},
  ...
]

Return JSON array only. No markdown. No explanation.
"""
    return [
        {"role": "system", "content": "You shorten texts without changing their meaning. Return JSON array only."},
        {"role": "user", "content": prompt},
    ]


def shorten_long_texts(los: list[dict], tasks: list[dict], batch_size: int = 5) -> dict[str, Any]:
    """Shortens the too-long texts of `los` and `tasks` in place. Returns {"long": n, "shortened": m}."""
    from lo_pipeline.llm.parallel import chunks, map_batches

    found = long_texts(los, tasks)
    if not found:
        return {"long": 0, "shortened": 0}
    logger.info(f"[Shorten] {len(found)} text(s) longer than their ECEH limit; asking the LLM to shorten them.")

    def one_batch(batch):
        try:
            response = call_llm(_prompt(batch), temperature=0.2)
            results = parse_json(response) if response else None
        except ValueError:  # json.JSONDecodeError is a ValueError
            logger.warning("[Shorten] Answer is not valid JSON; texts kept as they are.")
            return {}
        except LLMError as e:  # a timeout or crash is not caught
            logger.warning(f"[Shorten] LLM call failed ({e}); texts kept as they are.")
            return {}
        answers = {}
        for result in results if isinstance(results, list) else []:
            n = result.get("item") if isinstance(result, dict) else None
            if isinstance(n, int) and 1 <= n <= len(batch) and n not in answers:
                item = batch[n - 1]
                # HTML fields stay raw (units_output escapes them); names lose unsafe HTML
                text = _raw_text(result.get("text")) if item["as_html"] else _plain_text(result.get("text"))
                if text and len(text) < len(item["text"]):
                    answers[n] = text
        return answers

    batches = chunks(found, batch_size)
    shortened = 0
    for batch, answers in zip(batches, map_batches(one_batch, batches)):
        for n, text in answers.items():
            item = batch[n - 1]
            if item["index"] is not None:
                item["obj"][item["key"]][item["index"]] = text
            else:
                item["obj"][item["key"]] = text  # an LO hint loses its "[Source: …]" suffix (the source is its own hint)
            shortened += 1
    logger.info(f"[Shorten] {shortened} of {len(found)} text(s) shortened.")
    return {"long": len(found), "shortened": shortened}
