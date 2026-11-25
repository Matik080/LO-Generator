"""Choice questions with more than one correct option (sprint 2, task 1b).

Runs at the end of the `questions` stage, after the LOs were evaluated and filtered, so the core
(units, LOs, their MC options and evaluation, graph) is not changed by it.

For every LO with MC options (1 correct + 3 distractors, lo/generator.py), the LLM says whether the
question as written can have another correct answer according to the LO's source statement. If yes,
it writes that option and quotes the part of the source statement that supports it. The code keeps
the option only when
  - the quote really comes from the source statement (lexical overlap >= QUOTE_OVERLAP_MIN), and
  - it differs from every option the LO already has.
It then replaces the last distractor, so the LO keeps its number of options, and gets
`mc_correct_indices` (e.g. [0, 2]). `mc_correct_index` (the original answer) stays as it was.
Most LOs ask for one thing (a definition, a value) and get no second correct option.
"""

import logging
import re
from typing import Any, Dict, List

from lo_pipeline.llm.json_load import parse_json
from lo_pipeline.llm.service import call_llm
from lo_pipeline.lo.lexical import lexical_overlap, tokenize

logger = logging.getLogger(__name__)

QUOTE_OVERLAP_MIN = 0.9  # share of the quote's words that must appear in the source statement
QUOTE_MIN_WORDS = 3


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def has_mc_options(lo: Dict[str, Any]) -> bool:
    options, correct = lo.get("mc_options"), lo.get("mc_correct_index")
    return (isinstance(options, list) and len(options) >= 2 and isinstance(correct, int)
            and not isinstance(correct, bool) and 0 <= correct < len(options))


def apply_extra_correct(lo: Dict[str, Any], option, quote) -> bool:
    """Adds `option` as another correct answer when the checks pass. True when the LO changed."""
    if not isinstance(option, str) or not isinstance(quote, str) or not option.strip():
        return False
    option = option.strip()
    source = lo.get("source_statement") or ""
    if len(tokenize(quote)) < QUOTE_MIN_WORDS or lexical_overlap(source, quote) < QUOTE_OVERLAP_MIN:
        logger.info(f"[MultiCorrect] LO {lo.get('id')}: quote not found in the source statement; option dropped.")
        return False
    options = list(lo["mc_options"])
    if any(_normalized(option) == _normalized(str(o)) for o in options):
        return False
    correct = lo["mc_correct_index"]
    distractors = [i for i in range(len(options)) if i != correct]
    if not distractors:
        return False
    replaced = distractors[-1]
    options[replaced] = option
    lo["mc_options"] = options
    lo["mc_correct_indices"] = sorted([correct, replaced])
    return True


def _prompt(batch: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    los_text = ""
    for idx, lo in enumerate(batch):
        options = "\n".join(f"  {chr(65 + i)}) {o}" + ("  [correct]" if i == lo["mc_correct_index"] else "")
                            for i, o in enumerate(lo["mc_options"]))
        los_text += f"""
LO {idx + 1}:
Question: {lo['question']}
Source statement: {lo['source_statement']}
Options:
{options}
"""
    prompt = f"""
You are reviewing multiple choice questions. Each question has exactly one option marked [correct].

For each LO decide whether the question, exactly as written, can have ANOTHER correct answer that is
fully supported by the source statement (for example a second property, characteristic or example
the statement names).

Rules:
- Only use the source statement. No outside knowledge.
- If the question asks for one thing (a single definition, value, name or step), answer null.
- The extra option must be clearly correct, different from every existing option, and in the
  SAME LANGUAGE and similar length and style as the other options.
- "quote" must be copied word for word from the source statement and must support the extra option.
- When in doubt, answer null.

{los_text}

Return a JSON array with exactly {len(batch)} results in the same order:
[
  {{"lo": 1, "extra_correct": "another correct option" or null, "quote": "words from the source statement" or null}},
  ...
]

Return JSON array only. No markdown. No explanation.
"""
    return [
        {"role": "system",
         "content": "You check whether multiple choice questions can have more than one correct answer. "
                    "Return JSON array only."},
        {"role": "user", "content": prompt},
    ]


def add_correct_options(learning_objects: List[Dict[str, Any]], batch_size: int = 5, progress=None):
    """Second correct options for LOs with MC options (changes the LOs in place, returns the list).

    `progress(done, total)` is called after each LLM batch. Batches run in parallel (llm/parallel.py);
    each batch changes only its own LOs. A batch whose answer is not valid JSON changes nothing.
    """
    from lo_pipeline.llm.parallel import chunks, map_batches

    eligible = [lo for lo in learning_objects if has_mc_options(lo)]
    logger.info(f"[MultiCorrect] Checking {len(eligible)} choice LOs for another correct option...")

    def one_batch(batch):
        response = call_llm(_prompt(batch), temperature=0.2)
        if not response:
            return 0
        try:
            results = parse_json(response)
        except ValueError:  # json.JSONDecodeError is a ValueError
            logger.warning("[MultiCorrect] Answer is not valid JSON; batch left unchanged.")
            return 0
        if not isinstance(results, list):
            return 0
        changed = 0
        for result in results:
            if not isinstance(result, dict):
                continue
            idx = result.get("lo")
            if not isinstance(idx, int) or not 1 <= idx <= len(batch):
                continue
            changed += apply_extra_correct(batch[idx - 1], result.get("extra_correct"), result.get("quote"))
        return changed

    changed = sum(map_batches(one_batch, list(chunks(eligible, batch_size)), progress))
    logger.info(f"[MultiCorrect] Done. {changed} LO(s) got a second correct option.")
    return learning_objects
