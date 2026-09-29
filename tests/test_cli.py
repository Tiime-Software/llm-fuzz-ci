import json
from argparse import Namespace

from llm_fuzz_ci import cli
from llm_fuzz_ci.generator import Generated
from llm_fuzz_ci.schema import FuzzTarget, load_cases, make_case, write_cases, write_targets


def generate_args(tmp_path, **overrides):
    args = dict(
        agent="codex",
        model=None,
        provider=None,
        max_turns=None,
        max_budget_usd=None,
        timeout_seconds=600,
        show_usage=False,
        usage_report=None,
        dry_run=False,
        corpus_dir=tmp_path / "cases",
        targets=tmp_path / "targets.json",
    )
    return Namespace(**{**args, **overrides})


def summary_args(tmp_path, **overrides):
    args = dict(
        format="full",
        corpus_dir=tmp_path / "cases",
        report=tmp_path / "test-report.json",
        usage_report=tmp_path / "llm-usage.json",
        output=tmp_path / "reports" / "llm-fuzz-ci-report.md",
    )
    return Namespace(**{**args, **overrides})


def test_generate_replaces_stale_inputs(tmp_path, monkeypatch, capsys):
    write_targets(
        tmp_path / "targets.json",
        [FuzzTarget(id="divide", target="pytest::tests/test_app.py::test_divide", budget_usd=0.25)],
    )
    write_cases(tmp_path / "cases", [make_case(target_id="divide", input_value={"x": 1, "y": 0})])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "generate_cases", lambda *a, **k: Generated([]))

    assert cli.cmd_generate(generate_args(tmp_path)) == 0
    assert load_cases(tmp_path / "cases", "divide") == []


def test_generate_reports_inputs_it_could_not_parse(tmp_path, monkeypatch, capsys):
    write_targets(
        tmp_path / "targets.json",
        [FuzzTarget(id="divide", target="pytest::tests/test_app.py::test_divide", budget_usd=0.25)],
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "generate_cases",
        lambda *a, **k: Generated(
            [make_case(target_id="divide", input_value={"x": 1})],
            skipped=["divide input 2: not valid JSON"],
        ),
    )

    assert cli.cmd_generate(generate_args(tmp_path)) == 0
    assert "Discarded an unparseable input: divide input 2" in capsys.readouterr().out


def test_summary_writes_the_full_report(tmp_path, capsys):
    item = make_case(target_id="divide", input_value={"x": 1, "y": 0}, rationale="Zero divisor.")
    write_cases(tmp_path / "cases", [item])
    (tmp_path / "test-report.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-09-15T09:00:00Z",
                "exitstatus": 1,
                "results": [
                    {
                        "case_id": item.id,
                        "target_id": "divide",
                        "nodeid": "tests/test_app.py::test_divide[zero]",
                        "outcome": "failed",
                        "input": item.input,
                        "rationale": item.rationale,
                        "failure": "E   AssertionError: guard y=0",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    args = summary_args(tmp_path)

    assert cli.cmd_summary(args) == 0

    written = args.output.read_text(encoding="utf-8")
    assert written.startswith("# LLM Fuzz CI")
    assert "**1 of 1 tested inputs failed.**" in written
    assert "guard y=0" in written
    assert "<details>" not in written  # the artifact never folds
    assert str(args.output) in capsys.readouterr().out


def test_summary_overview_folds_and_points_at_the_artifact(tmp_path):
    write_cases(tmp_path / "cases", [make_case(target_id="divide", input_value={"x": 1})])
    args = summary_args(tmp_path, format="overview")

    assert cli.cmd_summary(args) == 0

    written = args.output.read_text(encoding="utf-8")
    assert "<details>" in written
    assert "llm-fuzz-ci-report" in written


def test_summary_runs_before_any_test_report_exists(tmp_path):
    write_cases(tmp_path / "cases", [make_case(target_id="divide", input_value={"x": 1})])
    args = summary_args(tmp_path)

    assert cli.cmd_summary(args) == 0
    assert "No test results yet." in args.output.read_text(encoding="utf-8")


def test_the_cli_exposes_only_the_commands_ci_uses():
    import argparse

    parser = cli.build_parser()
    commands = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )

    assert set(commands.choices) == {
        "collect",
        "generate",
        "test-fuzz-cases",
        "report",
        "summary",
    }


def test_generate_drops_inputs_for_a_test_that_was_renamed(tmp_path, monkeypatch, capsys):
    """A renamed test would otherwise leave inputs that read as `not tested` forever."""
    write_targets(
        tmp_path / "targets.json",
        [FuzzTarget(id="divide_v2", target="pytest::tests/test_app.py::test_divide_v2")],
    )
    write_cases(
        tmp_path / "cases",
        [
            make_case(target_id="divide_v1", input_value={"x": 1}),
            make_case(target_id="divide_v2", input_value={"x": 2}),
        ],
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "generate_cases",
        lambda *a, **k: Generated([make_case(target_id="divide_v2", input_value={"x": 3})]),
    )

    assert cli.cmd_generate(generate_args(tmp_path)) == 0

    assert load_cases(tmp_path / "cases", "divide_v1") == []
    assert [c.input for c in load_cases(tmp_path / "cases", "divide_v2")] == [{"x": 3}]
    assert "no longer exists: divide_v1.jsonl" in capsys.readouterr().out


def test_generate_marks_a_target_it_found_nothing_for(tmp_path, monkeypatch):
    write_targets(
        tmp_path / "targets.json",
        [FuzzTarget(id="quiet", target="pytest::tests/test_app.py::test_quiet")],
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "generate_cases", lambda *a, **k: Generated([]))

    assert cli.cmd_generate(generate_args(tmp_path)) == 0

    from llm_fuzz_ci.schema import case_file

    path = case_file(tmp_path / "cases", "quiet")
    assert path.exists() and path.read_text() == ""


def report_args(tmp_path, **overrides):
    args = dict(
        corpus_dir=str(tmp_path / "cases"),
        report=str(tmp_path / "test-report.json"),
        usage_report=str(tmp_path / "llm-usage.json"),
        output_dir=str(tmp_path / "reports"),
        create_issue=False,
        issue_assignees="",
        issue_labels="",
        hard_fail=False,
    )
    return Namespace(**{**args, **overrides})


def test_report_writes_both_renderings_and_the_step_summary(tmp_path, monkeypatch):
    case = make_case(target_id="t.py::a", input_value={"x": 1}, rationale="why")
    write_cases(tmp_path / "cases", [case])
    (tmp_path / "test-report.json").write_text(
        json.dumps(
            {
                "summary": {"failed_cases": 1},
                "exitstatus": 1,
                "results": [{"case_id": case.id, "outcome": "failed"}],
            }
        )
    )
    step_summary = tmp_path / "step.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))
    monkeypatch.chdir(tmp_path)

    assert cli.cmd_report(report_args(tmp_path)) == 0

    assert (tmp_path / "reports" / "overview.md").exists()
    assert (tmp_path / "reports" / "llm-fuzz-ci-report.md").exists()
    assert "LLM Fuzz CI" in step_summary.read_text()


def test_hard_fail_turns_a_failing_input_into_a_non_zero_exit(tmp_path, monkeypatch):
    case = make_case(target_id="t.py::a", input_value={"x": 1})
    write_cases(tmp_path / "cases", [case])
    (tmp_path / "test-report.json").write_text(
        json.dumps({"summary": {"failed_cases": 2}, "exitstatus": 1, "results": []})
    )
    monkeypatch.chdir(tmp_path)

    assert cli.cmd_report(report_args(tmp_path, hard_fail=True)) == 1
    assert cli.cmd_report(report_args(tmp_path, hard_fail=False)) == 0


def test_no_issue_is_attempted_without_a_token(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    cli.open_issue("body", 1, "", "")

    assert "permissions: issues: write" in capsys.readouterr().err
