"""pytest plugin: collect llm_fuzz targets, run saved inputs, write a report."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from .schema import (
    REPORT_SCHEMA_VERSION,
    case_file,
    FuzzCase,
    FuzzTarget,
    load_cases,
    utc_now,
    write_targets,
)


# A harness calling f(**llm_fuzz_case.input) raises this when the agent invents
# a key, or misses one. It says the input did not fit the function, not that the
# function is unsafe, so it must never be reported as a finding.
SIGNATURE_MISMATCH = re.compile(
    r"TypeError: .*\(\) got an unexpected keyword argument"
    r"|TypeError: .*\(\) missing \d+ required positional argument"
    r"|TypeError: .*\(\) takes \d+ positional arguments? but"
)


@dataclass(frozen=True)
class NoInputs:
    """Stands in for a target the generator produced nothing for."""

    target_id: str


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("llm-fuzz-ci")
    group.addoption(
        "--llm-fuzz-corpus-dir",
        default=".llm-fuzz/cases",
        help="Directory holding saved JSONL inputs.",
    )
    group.addoption(
        "--llm-fuzz-collect-targets",
        default=None,
        help="Write discovered @pytest.mark.llm_fuzz targets to this JSON file.",
    )
    group.addoption(
        "--llm-fuzz-report",
        default=".llm-fuzz/reports/test-report.json",
        help="Write per-input results to this JSON file.",
    )
    group.addoption(
        "--llm-fuzz-require-cases",
        action="store_true",
        default=False,
        help="Fail a marked test that has no saved inputs, instead of skipping it.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "llm_fuzz(budget_usd=None, params=None): mark a test as an LLM Fuzz CI "
        "target. params limits generation to those input keys.",
    )
    config.llm_fuzz_targets = []  # type: ignore[attr-defined]
    config.llm_fuzz_results = []  # type: ignore[attr-defined]


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    config.llm_fuzz_targets = [  # type: ignore[attr-defined]
        target_from_marker(item, marker)
        for item in items
        if (marker := item.get_closest_marker("llm_fuzz")) is not None
    ]


def pytest_collection_finish(session: pytest.Session) -> None:
    output = session.config.getoption("--llm-fuzz-collect-targets")
    if output:
        write_targets(output, session.config.llm_fuzz_targets)  # type: ignore[attr-defined]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "llm_fuzz_case" not in metafunc.fixturenames:
        return
    if metafunc.config.getoption("--llm-fuzz-collect-targets"):
        return

    marker = metafunc.definition.get_closest_marker("llm_fuzz")
    if marker is None:
        raise pytest.UsageError(
            "llm_fuzz_case requires @pytest.mark.llm_fuzz(budget_usd=...)"
        )

    target = target_from_marker(metafunc.definition, marker)
    corpus_dir = metafunc.config.getoption("--llm-fuzz-corpus-dir")
    cases = load_cases(corpus_dir, target.id)
    if cases:
        params = [pytest.param(case, id=case.id) for case in cases]
    elif case_file(corpus_dir, target.id).exists():
        # The agent looked at this target and found nothing worth testing.
        params = [
            pytest.param(
                NoInputs(target.id),
                marks=pytest.mark.skip(reason="The generator found no weakness to test"),
                id="no-weakness-found",
            )
        ]
    elif metafunc.config.getoption("--llm-fuzz-require-cases"):
        params = [pytest.param(NoInputs(target.id), id="no-inputs")]
    else:
        params = [
            pytest.param(
                NoInputs(target.id),
                marks=pytest.mark.skip(reason=f"No saved inputs for {target.id!r}"),
                id="no-inputs",
            )
        ]
    metafunc.parametrize("llm_fuzz_case", params, indirect=True)


@pytest.fixture
def llm_fuzz_case(request: pytest.FixtureRequest) -> FuzzCase:
    value = request.param
    request.node.llm_fuzz_param = value
    if isinstance(value, NoInputs):
        pytest.fail(f"No saved inputs for {value.target_id!r}")
    return value


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]):
    outcome = yield
    report = outcome.get_result()
    value = getattr(item, "llm_fuzz_param", None)
    if value is None:
        return

    # A missing corpus fails during fixture setup, never during the call. Record
    # it anyway, or a red run shows zero failures in the report.
    is_missing = isinstance(value, NoInputs)
    relevant = call.when == ("setup" if is_missing else "call")
    if not relevant:
        return

    outcome = report.outcome
    if report.failed and SIGNATURE_MISMATCH.search(report.longreprtext or ""):
        outcome = "invalid input"

    entry = {
        "nodeid": item.nodeid,
        "outcome": outcome,
        "duration": report.duration,
        "target_id": value.target_id,
        "case_id": f"{value.target_id}::no-inputs" if is_missing else value.id,
        "input": None if is_missing else value.input,
        "rationale": (
            "The generator produced no input for this test."
            if is_missing
            else value.rationale
        ),
    }
    if report.failed:
        entry["failure"] = report.longreprtext
    item.config.llm_fuzz_results.append(entry)  # type: ignore[attr-defined]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    output = session.config.getoption("--llm-fuzz-report")
    results = session.config.llm_fuzz_results  # type: ignore[attr-defined]
    targets = session.config.llm_fuzz_targets  # type: ignore[attr-defined]
    # The path is defaulted, so an ordinary `pytest` run in a repo with no
    # marked tests must not start leaving report files behind. A marked test
    # that skipped still counts: that run has something to report.
    if not output or (not results and not targets):
        return

    failed = [result for result in results if result["outcome"] == "failed"]
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "exitstatus": exitstatus,
        "summary": {
            "executed_cases": len(results),
            "failed_cases": len(failed),
            "passed_cases": sum(1 for r in results if r["outcome"] == "passed"),
        },
        "results": results,
    }
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def target_from_marker(item: pytest.Item, marker: pytest.Mark) -> FuzzTarget:
    if marker.args:
        raise pytest.UsageError(
            "@pytest.mark.llm_fuzz takes no positional arguments. "
            "Use @pytest.mark.llm_fuzz(budget_usd=0.25)."
        )
    unknown = set(marker.kwargs) - {"budget_usd", "params"}
    if unknown:
        raise pytest.UsageError(
            f"@pytest.mark.llm_fuzz got unknown arguments: {', '.join(sorted(unknown))}. "
            "It takes budget_usd and params."
        )
    source = getattr(item, "path", None) or getattr(item, "fspath", None)
    try:
        return FuzzTarget.from_dict(
            {
                "id": item.nodeid,
                "target": f"pytest::{item.nodeid}",
                "budget_usd": marker.kwargs.get("budget_usd"),
                "params": marker.kwargs.get("params"),
                "description": item_description(item),
                "test_nodeid": item.nodeid,
                "source_file": str(source) if source is not None else None,
            }
        )
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc


def item_description(item: pytest.Item) -> str | None:
    doc = getattr(getattr(item, "obj", None), "__doc__", None)
    return " ".join(doc.split()) if isinstance(doc, str) and doc.strip() else None
