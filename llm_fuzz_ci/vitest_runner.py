from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .schema import (
    REPORT_SCHEMA_VERSION,
    FuzzTarget,
    stable_case_id,
    utc_now,
    write_targets,
)


def vitest_command(paths: list[str]) -> list[str]:
    """Prefer a project-local vitest; fall back to whatever npx resolves."""
    local = Path("node_modules/.bin/vitest")
    base = [str(local)] if local.exists() else ["npx", "--no-install", "vitest"]
    return [*base, "run", "--reporter=dot", *paths]


SOURCE_SUFFIXES = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx"}


def target_files(paths: list[str]) -> list[str]:
    """The test files that declare a fuzz target.

    Collection must not run a project's ordinary tests, and vitest offers no
    version-stable way to import a file without running it: `vitest list`
    stopped executing module scope in vitest 5, which is where the helper
    registers itself. So the files are chosen here by looking for the call, and
    only those are handed to vitest -- everything else is never loaded.
    """
    found: list[str] = []
    for raw in paths:
        start = Path(raw)
        candidates = [start] if start.is_file() else sorted(start.rglob("*"))
        for path in candidates:
            if path.suffix not in SOURCE_SUFFIXES or "node_modules" in path.parts:
                continue
            try:
                if "fuzzTest" in path.read_text(encoding="utf-8", errors="ignore"):
                    found.append(str(path))
            except OSError:
                continue
    return found


def drain(directory: Path) -> list[dict[str, Any]]:
    """Every record the helper wrote.

    The order is arbitrary -- the filenames are uuids, and vitest writes them
    from parallel workers. Nothing downstream depends on it: the report walks
    the corpus and looks each result up by id.
    """
    if not directory.exists():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return records


MISSING_VITEST = (
    "vitest was not found in this project. Install it before running this:\n"
    "  npm install --save-dev vitest\n"
    "Without it npm answers with its own error, which says nothing about fuzzing."
)


def require_vitest() -> None:
    """Fail with one clear line rather than letting npm explain itself."""
    if Path("node_modules/.bin/vitest").exists():
        return
    if shutil.which("npx") is None:
        raise RuntimeError(MISSING_VITEST)
    probe = subprocess.run(
        ["npx", "--no-install", "vitest", "--version"],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        raise RuntimeError(MISSING_VITEST)


def collect(paths: list[str], output: str) -> int:
    """Discover fuzzTest targets and write them where `generate` looks.

    `vitest list` imports the test files without running them, which is what
    collection has to mean: pointing this at a directory must not execute the
    ordinary tests that live there. Older vitest has no `list`, so a run is the
    fallback -- and there the helper's own skip is all that holds tests back.
    """
    require_vitest()
    files = target_files(paths)
    if not files:
        return 0  # the caller reports "no marked tests", with the right advice

    with tempfile.TemporaryDirectory(prefix="llm-fuzz-collect-") as tmp:
        env = os.environ.copy()
        env["LLM_FUZZ_COLLECT_DIR"] = tmp
        completed = subprocess.run(vitest_command(files), env=env, check=False)
        targets = [FuzzTarget.from_dict(item) for item in drain(Path(tmp))]

    if completed.returncode != 0 and not targets:
        return completed.returncode
    write_targets(output, sorted(targets, key=lambda target: target.id))
    return 0


VITEST_RESULTS = ".llm-fuzz/reports/vitest-results"


def adopt_plain_run(report: str, results_dir: str = VITEST_RESULTS) -> bool:
    """Build the report from a `vitest run` the user launched themselves.

    The helper records every case whether or not this CLI started vitest, so a
    workflow can call the test command in the open. Nothing to adopt when this
    CLI already wrote the report itself.
    """
    if Path(report).exists():
        return False
    results = drain(Path(results_dir))
    if not results:
        return False
    write_report(report, results, 1 if any(r.get("outcome") == "failed" for r in results) else 0)
    return True


def run_cases(
    paths: list[str],
    *,
    corpus_dir: str,
    report: str,
    require_cases: bool,
) -> int:
    """Replay every saved input and write the report the summary reads."""
    require_vitest()
    with tempfile.TemporaryDirectory(prefix="llm-fuzz-results-") as tmp:
        env = os.environ.copy()
        env["LLM_FUZZ_RESULT_DIR"] = tmp  # keep our own run out of the tree
        env["LLM_FUZZ_CORPUS_DIR"] = corpus_dir
        if require_cases:
            env["LLM_FUZZ_REQUIRE_CASES"] = "1"
        completed = subprocess.run(vitest_command(paths), env=env, check=False)
        results = drain(Path(tmp))

    write_report(report, results, completed.returncode)
    return completed.returncode


def write_report(path: str, results: list[dict[str, Any]], exitstatus: int) -> Path:
    for item in results:
        item.setdefault(
            "case_id", stable_case_id(item.get("target_id", ""), item.get("input"))
        )
    failed = [item for item in results if item.get("outcome") == "failed"]
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "exitstatus": exitstatus,
        "summary": {
            "executed_cases": len(results),
            "failed_cases": len(failed),
            "passed_cases": sum(1 for r in results if r.get("outcome") == "passed"),
        },
        "results": results,
    }
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return output
