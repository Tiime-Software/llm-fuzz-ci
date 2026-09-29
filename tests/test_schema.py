import json

import pytest

from llm_fuzz_ci.schema import (
    AGENT_OUTPUT_SCHEMA,
    DEFAULT_CASE_RATIONALE,
    FuzzCase,
    FuzzTarget,
    load_cases,
    load_targets,
    make_case,
    write_cases,
    write_targets,
)


def test_targets_round_trip(tmp_path):
    path = tmp_path / "targets.json"
    write_targets(
        path,
        [
            FuzzTarget(
                id="divide",
                target="pytest::tests/test_app.py::test_divide",
                budget_usd=0.25,
            )
        ],
    )

    targets = load_targets(path)

    assert targets[0].id == "divide"
    assert targets[0].target == "pytest::tests/test_app.py::test_divide"
    assert targets[0].budget_usd == 0.25


def test_targets_may_omit_the_budget():
    target = FuzzTarget.from_dict({"id": "divide", "target": "app:divide"})

    assert target.budget_usd is None


def test_a_budget_must_be_positive_when_given():
    with pytest.raises(ValueError, match="greater than 0"):
        FuzzTarget.from_dict({"id": "divide", "target": "app:divide", "budget_usd": 0})



def test_cases_round_trip(tmp_path):
    case = make_case(
        target_id="divide",
        input_value={"x": 1, "y": 0},
        rationale="Division by zero",
    )

    write_cases(tmp_path, [case])
    cases = load_cases(tmp_path, "divide")

    assert len(cases) == 1
    assert cases[0].input == {"x": 1, "y": 0}
    stored = json.loads((tmp_path / "divide.jsonl").read_text(encoding="utf-8"))
    assert "id" not in stored
    assert "source" not in stored
    assert "created_at" not in stored
    assert stored["rationale"] == "Division by zero"


def test_default_case_storage_uses_the_default_rationale(tmp_path):
    case = make_case(
        target_id="divide",
        input_value={"x": 1, "y": 1},
    )

    write_cases(tmp_path, [case])

    assert (tmp_path / "divide.jsonl").read_text(encoding="utf-8") == (
        '{"target_id": "divide", "input": {"x": 1, "y": 1}, '
        '"rationale": "Generated adversarial input."}\n'
    )


def test_old_case_metadata_is_accepted_but_not_rewritten(tmp_path):
    path = tmp_path / "divide.jsonl"
    path.write_text(
        (
            '{"schema_version":"llm-fuzz.case.v1","id":"old-id",'
            '"target_id":"divide","input":{"x":1,"y":0},'
            '"category":"zero-denominator","rationale":"old",'
            '"source":"codex","created_at":"2026-09-14T00:00:00Z"}\n'
        ),
        encoding="utf-8",
    )

    old_case = load_cases(tmp_path, "divide")[0]
    write_cases(tmp_path, [old_case], merge=False)
    rewritten = path.read_text(encoding="utf-8")

    assert '"id"' not in rewritten
    assert '"schema_version"' not in rewritten
    assert '"source"' not in rewritten
    assert '"created_at"' not in rewritten
    assert '"category"' not in rewritten  # dropped on rewrite


def test_write_cases_can_replace_or_merge_existing_corpus(tmp_path):
    old_case = make_case(
        target_id="divide",
        input_value={"x": 1, "y": 0},
        rationale="old",
    )
    new_case = make_case(
        target_id="divide",
        input_value={"x": 4, "y": 2},
        rationale="new",
    )

    write_cases(tmp_path, [old_case], merge=False)
    write_cases(tmp_path, [new_case], merge=False)
    assert [case.input for case in load_cases(tmp_path, "divide")] == [{"x": 4, "y": 2}]

    write_cases(tmp_path, [old_case], merge=True)
    assert [case.input for case in load_cases(tmp_path, "divide")] == [
        {"x": 4, "y": 2},
        {"x": 1, "y": 0},
    ]


def test_agent_input_json_is_parsed_to_case_input():
    case = FuzzCase.from_dict(
        {
            "target_id": "divide",
            "input_json": '{"x": 1, "y": 0}',
            "rationale": "Division by zero",
        }
    )

    assert case.input == {"x": 1, "y": 0}


def test_missing_case_rationale_uses_the_default():
    case = FuzzCase.from_dict({"target_id": "divide", "input": {"x": 1, "y": 1}})

    assert case.rationale == DEFAULT_CASE_RATIONALE


def test_agent_schema_uses_string_encoded_inputs():
    case_schema = AGENT_OUTPUT_SCHEMA["properties"]["cases"]["items"]

    assert "input" not in case_schema["properties"]
    assert case_schema["properties"]["input_json"]["type"] == "string"
    assert case_schema["required"] == ["input_json", "rationale"]
    assert "target_id" not in case_schema["properties"]
