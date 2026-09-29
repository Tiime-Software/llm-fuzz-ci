import json
import subprocess

import pytest

from llm_fuzz_ci import vitest_runner
from llm_fuzz_ci.cli import resolve_runner
from llm_fuzz_ci.schema import stable_case_id


def test_javascript_paths_pick_vitest_without_being_told():
    assert resolve_runner("auto", ["tests/redirect.fuzz.test.mjs"]) == "vitest"
    assert resolve_runner("auto", ["src/a.ts", "src/b.tsx"]) == "vitest"


def test_python_paths_stay_on_pytest():
    assert resolve_runner("auto", ["tests/test_app.py"]) == "pytest"
    assert resolve_runner("auto", []) == "pytest"


def test_an_explicit_runner_wins_over_the_extension():
    assert resolve_runner("pytest", ["tests/a.test.ts"]) == "pytest"
    assert resolve_runner("vitest", ["tests/test_app.py"]) == "vitest"


def test_a_project_local_vitest_is_preferred(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
    (tmp_path / "node_modules" / ".bin" / "vitest").write_text("#!/bin/sh\n")

    assert vitest_runner.vitest_command(["tests"])[0].endswith("node_modules/.bin/vitest")


def test_npx_is_the_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert vitest_runner.vitest_command(["tests"])[:2] == ["npx", "--no-install"]


def test_the_case_id_is_derived_here_so_the_two_runners_agree(tmp_path):
    """The JS helper deliberately sends no case_id.

    It is a hash of the input, and a second implementation of it in JavaScript
    would drift from this one without anything failing loudly.
    """
    target = "tests/a.test.mjs::rejects a bad url"
    report = tmp_path / "test-report.json"

    vitest_runner.write_report(
        str(report),
        [{"target_id": target, "input": {"url": "//evil"}, "outcome": "failed"}],
        exitstatus=1,
    )

    written = json.loads(report.read_text(encoding="utf-8"))
    assert written["results"][0]["case_id"] == stable_case_id(target, {"url": "//evil"})
    assert written["summary"] == {
        "executed_cases": 1,
        "failed_cases": 1,
        "passed_cases": 0,
    }


def test_records_the_helper_wrote_are_read_back_whole(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"outcome": "passed"}))
    (tmp_path / "b.json").write_text("{ not json")

    assert vitest_runner.drain(tmp_path) == [{"outcome": "passed"}]


def test_a_missing_directory_is_no_results_rather_than_a_crash(tmp_path):
    assert vitest_runner.drain(tmp_path / "nothing-here") == []


def test_only_files_declaring_a_target_are_handed_to_vitest(tmp_path, monkeypatch):
    """Collection must not run the ordinary tests living beside the marked ones.

    vitest 5 stopped executing module scope under `vitest list`, so the helper
    could no longer register itself there. Choosing the files here is the part
    that does not depend on a vitest version.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "a.fuzz.test.mjs").write_text('fuzzTest("x", () => {})')
    (tmp_path / "tests" / "ordinary.test.mjs").write_text('test("y", () => {})')
    (tmp_path / "tests" / "notes.md").write_text("fuzzTest")

    found = vitest_runner.target_files(["tests"])

    assert found == ["tests/a.fuzz.test.mjs"]


def test_node_modules_is_never_scanned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    vendored = tmp_path / "tests" / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    (vendored / "index.mjs").write_text("export function fuzzTest() {}")

    assert vitest_runner.target_files(["tests"]) == []


def test_a_single_file_path_is_accepted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.fuzz.test.mjs").write_text('fuzzTest("x", () => {})')

    assert vitest_runner.target_files(["a.fuzz.test.mjs"]) == ["a.fuzz.test.mjs"]


def test_a_missing_vitest_is_named_rather_than_left_to_npm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(vitest_runner.shutil, "which", lambda name: "/usr/bin/npx")
    monkeypatch.setattr(
        vitest_runner.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, b"", b"npm error ..."),
    )

    with pytest.raises(RuntimeError, match="npm install --save-dev vitest"):
        vitest_runner.require_vitest()


def test_a_project_local_vitest_needs_no_probe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
    (tmp_path / "node_modules" / ".bin" / "vitest").write_text("#!/bin/sh\n")

    def explode(*a, **k):
        raise AssertionError("should not have shelled out")

    monkeypatch.setattr(vitest_runner.subprocess, "run", explode)
    vitest_runner.require_vitest()


def test_vitest_is_not_launched_at_all_when_no_file_declares_a_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(vitest_runner, "require_vitest", lambda: None)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "ordinary.test.mjs").write_text('test("y", () => {})')

    def explode(*a, **k):
        raise AssertionError("should not have launched vitest")

    monkeypatch.setattr(vitest_runner.subprocess, "run", explode)

    assert vitest_runner.collect(["tests"], str(tmp_path / "targets.json")) == 0


def test_a_vitest_run_the_user_launched_is_adopted(tmp_path):
    """The workflow may call `vitest run` in the open, not through this CLI."""
    results = tmp_path / "vitest-results"
    results.mkdir()
    (results / "a-0.json").write_text(
        json.dumps({"target_id": "t.py::a", "input": {"x": 1}, "outcome": "failed"})
    )
    report = tmp_path / "test-report.json"

    assert vitest_runner.adopt_plain_run(str(report), str(results)) is True

    written = json.loads(report.read_text())
    assert written["summary"]["failed_cases"] == 1
    assert written["exitstatus"] == 1


def test_a_report_this_cli_already_wrote_is_left_alone(tmp_path):
    report = tmp_path / "test-report.json"
    report.write_text('{"mine": true}')

    assert vitest_runner.adopt_plain_run(str(report), str(tmp_path / "none")) is False
    assert report.read_text() == '{"mine": true}'
