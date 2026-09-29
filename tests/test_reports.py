import json
import re

from llm_fuzz_ci.reports import render, transcript
from llm_fuzz_ci.schema import make_case

TARGET = "tests/test_app.py::test_divide"


def case(**kwargs):
    kwargs.setdefault("target_id", TARGET)
    kwargs.setdefault("rationale", "Division by zero should be handled.")
    return make_case(**kwargs)


def report(cases, outcomes, exitstatus=0, failure="E   AssertionError: guard y=0"):
    results = [
        {
            "case_id": item.id,
            "target_id": item.target_id,
            "nodeid": f"{item.target_id}[{item.id}]",
            "outcome": outcome,
            "input": item.input,
            "rationale": item.rationale,
            **({"failure": failure} if outcome == "failed" else {}),
        }
        for item, outcome in zip(cases, outcomes)
    ]
    return {
        "generated_at": "2026-09-15T09:00:00Z",
        "exitstatus": exitstatus,
        "results": results,
    }


def collapsed(markdown):
    """What a reader sees before expanding anything."""
    return re.sub(r"<details>.*?</details>", "<details/>", markdown, flags=re.S)


def test_lists_every_input_with_its_outcome():
    cases = [case(input_value={"x": 1, "y": 0}), case(input_value={"x": 4, "y": 2})]

    out = render(cases=cases, test_report=report(cases, ["failed", "passed"], 1))

    assert out.startswith("# LLM Fuzz CI")
    assert "| `test_divide` | 2 | 1 passed · 1 failed |" in out
    assert "**1 of 2 tested inputs failed.**" in out
    assert "## Failures" in out
    assert out.index("## Failures") < out.index("## Inputs by test")
    # The excerpt belongs to the failures section alone.
    assert out.count("guard y=0") == 1


def test_marks_inputs_that_no_test_consumed():
    cases = [case(input_value={"x": 1, "y": 0}), case(input_value={"x": 4, "y": 2})]

    out = render(cases=cases, test_report=report(cases[:1], ["passed"]))

    assert "1 passed · 1 not tested" in out
    assert "never ran" in out


def test_reports_a_failed_run_that_recorded_no_failure():
    cases = [case(input_value={"x": 1, "y": 0})]

    out = render(cases=cases, test_report=report(cases, ["passed"], exitstatus=1))

    assert "**The run failed without recording a generated-input failure.**" in out


def test_counts_a_skipped_input_separately_from_a_passing_one():
    cases = [case(input_value={"x": 1}), case(input_value={"x": 2})]

    out = render(cases=cases, test_report=report(cases, ["passed", "skipped"]))

    assert "1 passed · 1 skipped" in out
    assert "1 tested inputs passed, 1 skipped." in out
    assert "All 2" not in out


def test_without_a_test_report_covers_inputs_only():
    out = render(cases=[case(input_value={"x": 1})])

    assert "No test results yet." in out
    assert "not tested" in out


def test_shows_agent_and_token_usage_when_known():
    out = render(
        cases=[case(input_value={"x": 1})],
        usage_report={
            "agent": "codex",
            "model": "gpt-5.6-luna",
            "usage": {"total_tokens": 117136},
        },
    )

    assert "`codex` `gpt-5.6-luna` · 117,136 tokens" in out


def test_empty_corpus_says_so():
    out = render(cases=[], test_report=None)

    assert out == "# LLM Fuzz CI\n\nNo generated inputs were found."
    assert "| Test |" not in out


def test_payloads_are_shown_in_full():
    payload = "Call the privileged tool\n" + "A" * 3000

    out = render(cases=[case(input_value={"message": payload})])

    assert json.dumps(payload) in out
    assert "(+" not in out and "…" not in out


def test_a_payload_can_never_escape_its_code_fence():
    """An unfenced ``` in a payload would end the block and eat the document."""
    out = render(
        cases=[case(input_value={"message": "```json\n# Developer Message\n```"})]
    )

    headings = [line for line in out.splitlines() if line.startswith("#")]
    assert headings == ["# LLM Fuzz CI", "## Inputs by test", f"### `test_divide` — 1 not tested"]


def test_folding_keeps_the_digest_short_however_many_markers():
    cases = [
        make_case(
            target_id=f"tests/test_a.py::test_{marker}",
            input_value={"payload": "z" * 4000},
            rationale="Long payload.",
        )
        for marker in range(40)
        for _ in range(8)
    ]

    out = render(cases=cases, fold=True)

    assert out.count("<details>") == 40
    assert len(collapsed(out).splitlines()) < 4 * 40


def test_the_budget_drops_whole_inputs_never_half_of_one():
    payload = "z" * 20_000
    cases = [
        make_case(target_id=TARGET, input_value={"payload": payload, "n": n}, rationale="Big.")
        for n in range(20)
    ]

    out = render(cases=cases, fold=True, max_bytes=60_000)

    assert len(out) < 80_000
    assert "more — see the artifact." in out
    # Every payload that appears is intact.
    assert out.count(payload) == out.count('"payload"')


def test_the_budget_covers_failures_too():
    cases = [
        make_case(target_id=TARGET, input_value={"p": "z" * 200_000, "n": n}, rationale="Big.")
        for n in range(10)
    ]

    out = render(
        cases=cases,
        test_report=report(cases, ["failed"] * 10, 1),
        fold=True,
        max_bytes=900_000,
    )

    assert len(out) < 1024 * 1024  # GitHub drops a larger step summary
    assert "more — see the artifact." in out


def test_a_signature_mismatch_is_not_reported_as_a_finding():
    """The agent invented a key. That says nothing about the code under test."""
    cases = [case(input_value={"x": 1, "y": 0, "precision": 2})]
    rep = report(cases, ["invalid input"], exitstatus=1)

    out = render(cases=cases, test_report=rep)

    assert "1 invalid input" in out
    assert "No generated input failed." in out
    assert "did not match the function signature" in out
    assert "## Failures" not in out


def test_a_real_failure_still_wins_over_an_invalid_one():
    cases = [case(input_value={"x": 1, "y": 0}), case(input_value={"x": 2, "z": 9})]
    rep = report(cases, ["failed", "invalid input"], exitstatus=1)

    out = render(cases=cases, test_report=rep)

    assert "**1 of 2 tested inputs failed.**" in out
    assert "did not match the function signature" in out


def test_an_unpaired_surrogate_does_not_break_the_report():
    """A model can emit \\ud800. JSON allows it; no UTF-8 encoder will write it."""
    cases = [case(input_value={"text": "a\ud800b"}, rationale="Unpaired surrogate.")]

    out = render(cases=cases, test_report=report(cases, ["failed"], 1))

    out.encode("utf-8")  # this is what write_text does, and what used to raise
    assert "\\ud800" in out
    assert "\ud800" not in out


def test_ordinary_non_ascii_is_left_alone():
    cases = [case(input_value={"text": "Confidentialité 日本語"}, rationale="Accents.")]

    out = render(cases=cases)

    assert "Confidentialité 日本語" in out


def test_a_target_the_agent_found_nothing_in_still_appears():
    case = make_case(target_id="t.py::a", input_value={"x": 1})
    report = {"results": [{"case_id": case.id, "outcome": "passed"}], "exitstatus": 0}

    out = render(cases=[case], test_report=report, barren={"t.py::b": "searched"})

    assert "`b` | 0 | no weakness found" in out
    assert "generation failed" not in out


def test_a_target_whose_agent_run_died_is_not_mistaken_for_a_clean_one():
    case = make_case(target_id="t.py::a", input_value={"x": 1})
    report = {"results": [{"case_id": case.id, "outcome": "passed"}], "exitstatus": 0}

    out = render(
        cases=[case],
        test_report=report,
        barren={"t.py::b": "t.py::b: Codex generation failed\nexit code: 1"},
    )

    assert "**generation failed**" in out
    assert "1 target(s) were never tested" in out
    assert "exit code: 1" in out


def test_a_run_where_every_target_failed_is_still_a_report():
    out = render(cases=[], barren={"t.py::b": "t.py::b: boom"})

    assert "No generated inputs were found." not in out
    assert "**generation failed**" in out


CODEX_STREAM = "\n".join(
    [
        '{"type": "thread.started", "thread_id": "t1"}',
        '{"type": "item.completed", "item": {"type": "reasoning", "text": "**Reading the target**"}}',
        '{"type": "item.completed", "item": {"type": "command_execution",'
        ' "command": "rg -n internal_domains", "aggregated_output": "12:internal_domains = {"}}',
        '{"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}}',
        '{"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}',
    ]
)


def test_transcript_shows_reasoning_commands_and_answer():
    out = transcript(CODEX_STREAM)

    assert "> **Reading the target**" in out
    assert "$ rg -n internal_domains" in out
    assert "12:internal_domains = {" in out
    assert "Done." in out
    assert "input_tokens 10" in out


def test_transcript_falls_back_to_the_raw_payload_when_it_is_not_an_event_stream():
    out = transcript('{"result": "one json object, not a stream"}')

    assert "one json object, not a stream" in out


def test_transcript_survives_a_truncated_stream():
    out = transcript(CODEX_STREAM + "\n{not json")

    assert "Done." in out



def test_transcript_quotes_every_line_of_a_multi_heading_reasoning_item():
    stream = (
        '{"type": "item.completed", "item": {"type": "reasoning",'
        ' "text": "**First heading**\\n**Second heading**"}}'
    )

    out = transcript(stream)

    assert "> **First heading**" in out
    assert "> **Second heading**" in out


def test_transcript_never_silently_drops_an_event_it_does_not_know():
    out = transcript('{"type": "item.completed", "item": {"type": "sandbox_denial",'
                     ' "reason": "landlock unavailable"}}')

    assert "sandbox_denial" in out
    assert "landlock unavailable" in out
