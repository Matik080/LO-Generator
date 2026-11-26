"""Output language (task 3): language stage, the prompt rule for another language, texts written by code."""
from conftest import FIXTURES
from lo_pipeline.contract.options import JobOptions, resolve_options
from lo_pipeline.llm import service as llm_service
from lo_pipeline.llm.service import call_llm, output_language_rule, use_output_language
from lo_pipeline.questions.units_output import build_units_and_report
from lo_pipeline.runner import PipelineOptions, run_pipeline

DOC = FIXTURES / "c_memory.html"  # English


def test_rule_only_when_the_languages_differ():
    assert output_language_rule("en", "en") is None and output_language_rule("sk", "sk") is None
    rule = output_language_rule("sk", "en")
    assert "written in Slovak" in rule and "source material is in English" in rule
    assert "written in English" in output_language_rule("en", "sk")


def test_rule_compares_with_the_language_of_the_units():
    from lo_pipeline.language import source_language
    # Slovak document whose units the LLM wrote in English: Slovak output needs the rule
    slovak_doc = {"document": "sk", "output": "sk", "units": "en"}
    assert source_language(slovak_doc) == "en"
    assert "written in Slovak" in output_language_rule(slovak_doc["output"], source_language(slovak_doc))
    # units kept in Slovak, or a result from before the units language was stored: no rule
    assert output_language_rule("sk", source_language({"document": "sk", "output": "sk", "units": "sk"})) is None
    assert output_language_rule("sk", source_language({"document": "sk", "output": "sk"})) is None


def test_call_llm_appends_the_rule_only_inside_the_block(monkeypatch):
    sent = []
    monkeypatch.setattr(llm_service, "_backend", lambda messages, **kw: sent.append(messages) or "ok")
    prompt = [{"role": "system", "content": "S"}, {"role": "user", "content": "Write a question."}]
    call_llm(prompt)
    with use_output_language("RULE"):
        call_llm(prompt)
    with use_output_language(None):
        call_llm(prompt)
    assert [m[-1]["content"] for m in sent] == ["Write a question.", "Write a question.\n\nRULE", "Write a question."]
    assert sent[1][0] == prompt[0] and prompt[-1]["content"] == "Write a question."  # the caller's list is unchanged


def test_language_stage_and_rule_only_in_the_stages_that_write_for_students(fake_llm, monkeypatch):
    """English document, Slovak questions requested: units stay English, LO/question prompts get the rule."""
    rule = output_language_rule("sk", "en")
    with_rule, without_rule = [], []

    def backend(messages, **kwargs):  # answers from the cassette (recorded without the rule)
        content = messages[-1]["content"]
        if content.endswith("\n\n" + rule):
            with_rule.append(content)
            messages = messages[:-1] + [{**messages[-1], "content": content[:-len("\n\n" + rule)]}]
        else:
            without_rule.append(content)
        return fake_llm(messages, **kwargs)

    monkeypatch.setattr(llm_service, "_backend", backend)
    opts = PipelineOptions.from_settings()
    opts.language = "sk"
    result = run_pipeline([DOC], options=opts)

    assert result["language"] == {"document": "en", "confident": True, "requested": "sk", "output": "sk",
                                  "units": "en"}
    # unit extraction (and the graph check, no student text) stay in the documents' language
    assert any("extracting atomic knowledge units" in p for p in without_rule)
    assert not any("extracting atomic knowledge units" in p for p in with_rule)
    assert any("generating pedagogically sound" in p for p in with_rule)  # LO generation
    assert any("naming lessons" in p for p in with_rule)  # lesson titles (split)


def test_default_auto_sends_the_old_prompts(fake_llm):
    # the cassette has only prompts without a rule: any rule would fail this run
    result = run_pipeline([DOC])
    assert result["language"]["output"] == "en" and result["language"]["requested"] == "auto"


def lo(**fields):
    base = {"id": "a", "type": "definition", "question": "What is a pointer?", "answer": "An address.",
            "hint": "Think. [Source: c.pdf, p. 3]", "source_statement": "S.", "source_title": "c.pdf",
            "source_page": 3, "keywords": [], "mc_options": ["x", "y", "z", "w"], "mc_correct_index": 0,
            "prerequisite_ids": [], "generated_name": "Pointer", "time_to_handle": 45, "time_to_explain": 30}
    base.update(fields)
    return base


def build(result_language=None, requested="auto"):
    result = {"learning_objects": [lo()]}
    if result_language:
        result["language"] = {"output": result_language}
    return build_units_and_report(result, resolve_options(JobOptions.model_validate({"language": requested})))


def test_texts_written_by_code_follow_the_output_language():
    units, _ = build("en")
    assert units["units"][0]["questions"][0]["hints"] == [
        {"name": "Source", "url": "<p>c.pdf, page 3</p>"}, {"name": "Hint", "url": "<p>Think.</p>"}]
    units, _ = build("sk")
    assert [h["name"] for h in units["units"][0]["questions"][0]["hints"]] == ["Zdroj", "Nápoveda"]
    # no language stage in the result (older results): the requested language, else Slovak
    assert build(requested="en")[0]["units"][0]["questions"][0]["hints"][0]["url"] == "<p>c.pdf, page 3</p>"
    assert build()[0]["units"][0]["questions"][0]["hints"][0]["url"] == "<p>c.pdf, strana 3</p>"
