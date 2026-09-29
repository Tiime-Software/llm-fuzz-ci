"""Command line entry point for LLM Fuzz CI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .generator import build_prompt, generate_cases
from .reports import SUMMARY_BYTES, render
from .schema import case_file, load_cases, load_targets, write_cases
from .usage import format_usage_summary, write_usage_report

TARGETS = ".llm-fuzz/targets.json"
CORPUS = ".llm-fuzz/cases"
TEST_REPORT = ".llm-fuzz/reports/test-report.json"
USAGE_REPORT = ".llm-fuzz/reports/llm-usage.json"
BARREN_REPORT = ".llm-fuzz/reports/no-inputs.json"
TRACES = ".llm-fuzz/reports/agent-trace"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-fuzz-ci")
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="Find marked fuzz targets")
    collect.add_argument("paths", nargs="*", default=["tests"])
    collect.add_argument("--output", default=TARGETS)
    add_runner_option(collect)
    collect.set_defaults(func=cmd_collect)

    generate = commands.add_parser("generate", help="Generate saved fuzz inputs")
    generate.add_argument("--targets", default=TARGETS)
    generate.add_argument("--corpus-dir", default=CORPUS)
    generate.add_argument("--agent", choices=["codex", "claude"], default="codex")
    generate.add_argument("--model", default=None)
    generate.add_argument(
        "--provider",
        default=None,
        help="Codex model provider id. Use 'openrouter' for the built-in shortcut.",
    )
    generate.add_argument(
        "--max-turns", type=int, default=None, help="Claude Code turns per target."
    )
    generate.add_argument(
        "--max-budget-usd",
        type=float,
        default=None,
        help="Override every marker budget. Enforced on Claude Code only.",
    )
    generate.add_argument("--timeout-seconds", type=int, default=600)
    generate.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be sent to the agent and exit, without spending.",
    )
    generate.add_argument("--show-usage", action="store_true")
    generate.add_argument("--usage-report", default=None)
    generate.set_defaults(func=cmd_generate)

    test = commands.add_parser("test-fuzz-cases", help="Run the saved inputs")
    test.add_argument("--corpus-dir", default=CORPUS)
    add_runner_option(test)
    test.add_argument("--report", default=TEST_REPORT)
    test.add_argument("--require-cases", action="store_true")
    test.add_argument("pytest_args", nargs=argparse.REMAINDER)
    test.set_defaults(func=cmd_test_fuzz_cases)

    report = commands.add_parser(
        "report", help="Render the run, post an issue, and set the exit code"
    )
    report.add_argument("--corpus-dir", default=CORPUS)
    report.add_argument("--report", default=TEST_REPORT)
    report.add_argument("--usage-report", default=USAGE_REPORT)
    report.add_argument("--output-dir", default=".llm-fuzz/reports")
    report.add_argument("--create-issue", action="store_true")
    report.add_argument("--issue-assignees", default="")
    report.add_argument("--issue-labels", default="")
    report.add_argument(
        "--hard-fail",
        action="store_true",
        help="Exit non-zero when a generated input failed its test.",
    )
    report.set_defaults(func=cmd_report)

    summary = commands.add_parser("summary", help="Render the run as Markdown")
    summary.add_argument(
        "--format",
        choices=["full", "overview"],
        default="full",
        help="full: every input, for the artifact. overview: a digest, for CI.",
    )
    summary.add_argument("--corpus-dir", default=CORPUS)
    summary.add_argument("--report", default=TEST_REPORT)
    summary.add_argument("--usage-report", default=USAGE_REPORT)
    summary.add_argument("--output", default=".llm-fuzz/reports/llm-fuzz-ci-report.md")
    summary.set_defaults(func=cmd_summary)

    return parser


def add_runner_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--runner",
        choices=["auto", "pytest", "vitest"],
        default="auto",
        help="Test framework to drive. auto picks vitest for .js/.ts paths.",
    )


def resolve_runner(choice: str, paths: list[str]) -> str:
    """Which framework owns these paths.

    Tests live in one language per path, and the extension says which. Only
    a mixed repo has to say so explicitly.
    """
    if choice != "auto":
        return choice
    suffixes = {Path(path).suffix for path in paths}
    if suffixes & {".js", ".mjs", ".ts", ".mts", ".jsx", ".tsx"}:
        return "vitest"
    # `any(rglob(...))` stops at the first hit; listing it walks the whole
    # tree, and a monorepo test directory is a slow thing to walk twice.
    for path in paths:
        directory = Path(path)
        if directory.is_dir() and not any(directory.rglob("*.py")):
            return "vitest"
    return "pytest"


def cmd_collect(args: argparse.Namespace) -> int:
    if resolve_runner(args.runner, args.paths) == "vitest":
        from . import vitest_runner

        if vitest_runner.collect(args.paths, args.output) != 0:
            print(
                "\nvitest could not run those paths. Check the project builds "
                "and that\nvitest is installed (`npm install --save-dev vitest`).",
                file=sys.stderr,
            )
            return 1
        return report_targets(args)

    import pytest

    status = pytest.main(
        [
            *plugin_args(),
            "--collect-only",
            "-q",
            f"--llm-fuzz-collect-targets={args.output}",
            *args.paths,
        ]
    )
    if status != 0:
        print(
            "\npytest could not collect those paths. If it could not import "
            "your code,\ninstall the project before running this, or set "
            "PYTHONPATH to where it lives.",
            file=sys.stderr,
        )
        return int(status)

    return report_targets(args)


MARKER_HELP = {
    "pytest": (
        "  - check the test is marked: @pytest.mark.llm_fuzz\n"
        "  - check the marked test takes the llm_fuzz_case argument"
    ),
    "vitest": (
        "  - check the test uses fuzzTest() from llm-fuzz-ci\n"
        "  - check the file is one vitest already runs"
    ),
}


def report_targets(args: argparse.Namespace) -> int:
    targets = load_targets(args.output)
    if not targets:
        # Silently collecting nothing is the most common first-run mistake, and
        # every later step would still be green.
        runner = resolve_runner(args.runner, args.paths)
        print(
            f"No marked tests found under {' '.join(args.paths)!r}.\n"
            "  - check the path is right\n" + MARKER_HELP[runner],
            file=sys.stderr,
        )
        return 1

    print(f"Found {len(targets)} marked test(s):")
    for target in targets:
        print(f"  {target.id}")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    targets = load_targets(args.targets)
    if args.dry_run:
        for target in targets:
            print(f"--- {target.id} " + "-" * max(0, 68 - len(target.id)))
            print(build_prompt(target, Path.cwd()))
        print(f"\n{len(targets)} agent run(s) would be made. Nothing was sent.")
        return 0
    print(
        f"Generating inputs for {len(targets)} target(s) with {args.agent}"
        f" — one agent run each.",
        flush=True,
    )
    result = generate_cases(
        targets,
        agent=args.agent,
        repo_root=Path.cwd(),
        model=args.model,
        provider=args.provider,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget_usd,
        timeout_seconds=args.timeout_seconds,
        capture_usage=args.show_usage or bool(args.usage_report),
        trace_dir=Path(TRACES),
    )

    dropped = clear_corpus(args.corpus_dir, targets)
    written = write_cases(args.corpus_dir, result.cases)
    written += mark_searched(args.corpus_dir, targets, result.cases)

    for path in dropped:
        print(f"Removed inputs for a test that no longer exists: {path.name}")

    print(f"Generated {len(result.cases)} input(s) across {len(written)} file(s).")
    print(f"Agent transcripts written to {TRACES}/.")
    for path in written:
        print(path)
    for reason in result.skipped:
        print(f"Discarded an unparseable input: {reason}")
    for target_id, reason in result.failures.items():
        print(f"Generation failed for {target_id}: {reason}")

    write_barren_report(BARREN_REPORT, targets, result)
    if args.show_usage:
        print(format_usage_summary(result.usage))
    if args.usage_report:
        path = write_usage_report(
            args.usage_report,
            agent=args.agent,
            model=args.model,
            provider=args.provider,
            target_count=len(targets),
            case_count=len(result.cases),
            usage=result.usage,
        )
        print(f"Wrote token usage to {path}.")
    return 0


def cmd_test_fuzz_cases(args: argparse.Namespace) -> int:
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    forwarded = list(args.pytest_args)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]

    if resolve_runner(args.runner, forwarded) == "vitest":
        from . import vitest_runner

        return vitest_runner.run_cases(
            forwarded,
            corpus_dir=args.corpus_dir,
            report=args.report,
            require_cases=args.require_cases,
        )

    import pytest

    # Verbosity is the runner's own flag, so it is chosen here rather than by
    # the caller: a `-q` meant for pytest reaches vitest as a path.
    if not any(arg.startswith(("-q", "-v", "--verbos", "--quiet")) for arg in forwarded):
        forwarded.append("-q")

    options = [
        *plugin_args(),
        f"--llm-fuzz-corpus-dir={args.corpus_dir}",
        f"--llm-fuzz-report={args.report}",
    ]
    if args.require_cases:
        options.append("--llm-fuzz-require-cases")
    return pytest.main(options + (forwarded or ["tests"]))


def cmd_summary(args: argparse.Namespace) -> int:
    from . import vitest_runner

    # A workflow may run vitest itself rather than through this CLI.
    vitest_runner.adopt_plain_run(args.report)

    overview = args.format == "overview"
    content = render(
        cases=load_cases(args.corpus_dir),
        test_report=read_json(args.report),
        usage_report=read_json(args.usage_report),
        barren=read_json(BARREN_REPORT) or None,
        fold=overview,
        max_bytes=SUMMARY_BYTES if overview else None,
    )
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")
    print(f"Wrote {path}.")
    return 0


def mark_searched(corpus_dir: str | Path, targets: list[Any], cases: list[Any]) -> list[Path]:
    """Leave an empty file for a target the agent found no weakness in.

    Without it the test run cannot tell "the agent looked and found nothing"
    from "generation never happened", and would fail the build for both.
    """
    covered = {case.target_id for case in cases}
    empty = []
    for target in targets:
        if target.id in covered:
            continue
        path = case_file(corpus_dir, target.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        empty.append(path)
    return empty


def clear_corpus(corpus_dir: str | Path, targets: list[Any]) -> list[Path]:
    """Empty the corpus before regenerating it.

    Files for the current targets are replaced. Files for targets that no
    longer exist are deleted, or a renamed test leaves inputs behind that every
    later run reports as `not tested`.
    """
    root = Path(corpus_dir)
    if not root.exists():
        return []
    keep = {case_file(root, target.id) for target in targets}
    stale = []
    for path in sorted(root.glob("*.jsonl")):
        path.unlink()
        if path not in keep:
            stale.append(path)
    return stale


def plugin_args() -> list[str]:
    """Load the plugin once, by module, whether or not it is pip-installed."""
    return ["-p", "no:llm_fuzz_ci", "-p", "llm_fuzz_ci.pytest_plugin"]


def read_json(path: str | Path) -> dict[str, Any] | None:
    file = Path(path)
    return json.loads(file.read_text(encoding="utf-8")) if file.exists() else None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


def write_barren_report(path: str, targets: list[Any], result: Any) -> None:
    """Record why each target that produced no inputs produced none.

    An empty corpus on its own cannot say whether the agent looked and found
    nothing or never got an answer out of the model, and those need different
    reactions from whoever reads the summary.
    """
    covered = {case.target_id for case in result.cases}
    barren = {
        target.id: result.failures.get(target.id, "searched")
        for target in targets
        if target.id not in covered
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(barren, indent=2), encoding="utf-8")


def cmd_report(args: argparse.Namespace) -> int:
    """Everything a CI run wants after the tests: show, keep, alert, fail.

    A command rather than a second action, so a workflow reads as one action of
    ours and then ordinary steps. Uploading the artifact is the only piece left
    outside, because only GitHub's own action can do it.
    """
    from . import vitest_runner

    vitest_runner.adopt_plain_run(args.report)

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    rendered = {}
    for name, overview in (("overview.md", True), ("llm-fuzz-ci-report.md", False)):
        rendered[name] = render(
            cases=load_cases(args.corpus_dir),
            test_report=read_json(args.report),
            usage_report=read_json(args.usage_report),
            barren=read_json(BARREN_REPORT) or None,
            fold=overview,
            max_bytes=SUMMARY_BYTES if overview else None,
        )
        (out / name).write_text(rendered[name] + "\n", encoding="utf-8")

    overview = rendered["overview.md"]
    print(overview)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(overview + "\n")

    failed = int((read_json(args.report) or {}).get("summary", {}).get("failed_cases", 0))
    if failed and args.create_issue:
        open_issue(overview, failed, args.issue_assignees, args.issue_labels)
    if failed and args.hard_fail:
        print(f"{failed} generated input(s) failed a marked test.", file=sys.stderr)
        return 1
    return 0


def names(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def open_issue(body: str, failed: int, assignees: str, labels: str) -> None:
    """Open a GitHub issue for the run, if the environment can."""
    import json as _json
    import urllib.error
    import urllib.request

    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not repo or not token:
        print(
            "Not opening an issue: GITHUB_REPOSITORY and GITHUB_TOKEN must both "
            "be set, and the job needs `permissions: issues: write`.",
            file=sys.stderr,
        )
        return

    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    sha = os.environ.get("GITHUB_SHA", "")[:7]
    payload = {
        "title": f"LLM Fuzz CI: {failed} generated input(s) failing",
        "body": f"{body}\n\n[Run]({server}/{repo}/actions/runs/{run_id}) - commit `{sha}`",
        "assignees": names(assignees),
        "labels": names(labels),
    }
    request = urllib.request.Request(
        f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}/repos/{repo}/issues",
        data=_json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            number = _json.loads(response.read()).get("number")
        print(f"Opened issue #{number}.")
    except urllib.error.HTTPError as exc:
        print(f"Could not open the issue: {exc.code} {exc.read()[:200]!r}", file=sys.stderr)
