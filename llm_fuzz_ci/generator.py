from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from importlib.resources import files
from pathlib import Path
from typing import Iterable

from .reports import code_block, transcript
from .schema import (
    AGENT_OUTPUT_SCHEMA,
    FuzzCase,
    FuzzTarget,
    make_case,
    sanitize_target_id,
)
from .usage import LLMUsage, extract_usage_from_json_events

@dataclass
class Generated:
    cases: list[FuzzCase]
    usage: LLMUsage | None = None
    skipped: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)


def generate_cases(
    targets: Iterable[FuzzTarget],
    *,
    agent: str,
    repo_root: str | Path,
    model: str | None = None,
    provider: str | None = None,
    max_turns: int | None = None,
    max_budget_usd: float | None = None,
    timeout_seconds: int = 600,
    capture_usage: bool = False,
    trace_dir: Path | None = None,
) -> Generated:
    if provider and agent != "codex":
        raise ValueError(
            "--provider currently applies only to --agent codex. For Claude Code, "
            "configure Anthropic, Bedrock, Vertex, Foundry, or an API gateway through "
            "Claude Code environment variables."
        )

    target_list = list(targets)
    if max_budget_usd is not None:
        if max_budget_usd <= 0:
            raise ValueError("--max-budget-usd must be greater than 0")
        target_list = [replace(target, budget_usd=max_budget_usd) for target in target_list]

    if agent not in {"codex", "claude"}:
        raise ValueError(f"Unsupported agent: {agent}")

    cases: list[FuzzCase] = []
    skipped: list[str] = []
    failures: dict[str, str] = {}
    usage: LLMUsage | None = None
    root = Path(repo_root)

    for number, target in enumerate(target_list, start=1):
        print(f"[{number}/{len(target_list)}] {target.id}", flush=True)
        try:
            if agent == "codex":
                result = generate_with_codex(
                    target,
                    root,
                    model=model,
                    provider=provider,
                    timeout_seconds=timeout_seconds,
                    capture_usage=capture_usage,
                    trace_dir=trace_dir,
                )
            else:
                result = generate_with_claude(
                    target,
                    root,
                    model=model,
                    max_turns=max_turns,
                    timeout_seconds=timeout_seconds,
                    capture_usage=capture_usage,
                    trace_dir=trace_dir,
                )
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            # Targets are independent. Losing one must not discard the inputs
            # already paid for, nor stop the targets after it.
            failures[target.id] = str(exc)
            continue
        cases.extend(result.cases)
        skipped.extend(result.skipped)
        usage = merge_usage(usage, result.usage)

    return Generated(cases, usage, skipped, failures)


LANGUAGE_NAMES = {"python": "Python", "javascript": "JavaScript"}


def build_prompt(target: FuzzTarget, repo_root: Path) -> str:
    template = files("llm_fuzz_ci.prompts").joinpath("generate_cases.md").read_text()
    return (
        template.replace("{{REPO_ROOT}}", str(repo_root))
        .replace("{{TARGET_JSON}}", json.dumps(target.to_dict(), indent=2))
        .replace("{{LANGUAGE}}", language_name(target))
        .replace("{{FRAMEWORK}}", framework_name(target))
        .replace("{{EXAMPLE_INPUT_JSON}}", json.dumps(json.dumps({"x": 1, "y": 0})))
        .replace("{{INPUT_KEYS}}", input_keys_rule(target))
    )


def language_name(target: FuzzTarget) -> str:
    return LANGUAGE_NAMES.get(target.language.lower(), target.language)


def framework_name(target: FuzzTarget) -> str:
    """The test runner that will replay these inputs, named as the user knows it."""
    prefix = str(target.target).split("::", 1)[0]
    return prefix if prefix in {"pytest", "vitest"} else "pytest"


def input_keys_rule(target: FuzzTarget) -> str:
    """Tell the agent which keys to produce, exactly when the marker said so."""
    if target.params is None:
        return (
            "- Infer the input_json keys from the marked test itself: how it "
            "reads the input it is handed, and the call it makes with it."
        )
    names = ", ".join(json.dumps(name) for name in target.params)
    return (
        f"- Every input_json must contain exactly these keys and no others: {names}.\n"
        "- Vary the values, never the key names."
    )


def merge_usage(total: LLMUsage | None, item: LLMUsage | None) -> LLMUsage | None:
    if item is None:
        return total
    if total is None:
        return item
    total.add(item)
    return total


def save_trace(
    trace_dir: Path | None,
    target: FuzzTarget,
    stream: str,
    errors: str = "",
) -> None:
    """Keep what the agent did, next to what it produced.

    Two files: the provider's event stream verbatim, and a readable rendering
    of it. Without them a surprising set of inputs cannot be explained -- there
    is no way to tell whether the code was read or the values were guessed.
    """
    if trace_dir is None or not stream.strip():
        return
    trace_dir.mkdir(parents=True, exist_ok=True)
    name = sanitize_target_id(target.id)
    (trace_dir / f"{name}.jsonl").write_text(
        stream, encoding="utf-8", errors="backslashreplace"
    )
    body = transcript(stream)
    if errors.strip():
        body += "\n\n## stderr\n\n" + "\n".join(code_block(errors.strip(), "text"))
    (trace_dir / f"{name}.md").write_text(
        f"# {target.id}\n\n{body}\n", encoding="utf-8", errors="backslashreplace"
    )


def generate_with_codex(
    target: FuzzTarget,
    repo_root: Path,
    *,
    model: str | None,
    provider: str | None,
    timeout_seconds: int,
    capture_usage: bool,
    trace_dir: Path | None,
) -> Generated:
    help_text = require_codex()
    prompt = build_prompt(target, repo_root)
    with tempfile.TemporaryDirectory(prefix="llm-fuzz-codex-") as tmpdir:
        tmp = Path(tmpdir)
        schema_path = tmp / "agent-output.schema.json"
        output_path = tmp / "agent-output.json"
        schema_path.write_text(json.dumps(AGENT_OUTPUT_SCHEMA), encoding="utf-8")

        cmd = codex_command(
            help_text=help_text,
            schema_path=schema_path,
            output_path=output_path,
            model=model,
            provider=provider,
            capture_usage=capture_usage,
        )
        cmd.append(prompt)
        env = codex_env(provider)

        completed = subprocess.run(
            cmd,
            cwd=repo_root,
            env=env,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=False,
        )
        save_trace(trace_dir, target, completed.stdout, completed.stderr)
        if completed.returncode != 0:
            raise RuntimeError(describe_failure("Codex", cmd, completed))

        output_text = (
            output_path.read_text(encoding="utf-8")
            if output_path.exists()
            else completed.stdout
        )
        cases, skipped = parse_agent_cases(output_text, target)
        return Generated(
            cases,
            extract_usage_from_json_events(
                completed.stdout,
                provider=usage_provider(provider),
                model=model,
            )
            if capture_usage
            else None,
            skipped,
        )


def generate_with_claude(
    target: FuzzTarget,
    repo_root: Path,
    *,
    model: str | None,
    max_turns: int | None,
    timeout_seconds: int,
    capture_usage: bool,
    trace_dir: Path | None,
) -> Generated:
    prompt = build_prompt(target, repo_root)
    cmd = claude_command(
        prompt=prompt,
        model=model,
        max_turns=max_turns,
        max_budget_usd=target.budget_usd,
    )
    env = os.environ.copy()
    completed = subprocess.run(
        cmd,
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout_seconds,
        check=False,
    )
    save_trace(trace_dir, target, completed.stdout, completed.stderr)
    if completed.returncode != 0:
        raise RuntimeError(describe_failure("Claude", cmd, completed))
    cases, skipped = parse_agent_cases(completed.stdout, target)
    return Generated(
        cases,
        extract_usage_from_json_events(
            completed.stdout,
            provider="anthropic",
            model=model,
        )
        if capture_usage
        else None,
        skipped,
    )


def claude_command(
    *,
    prompt: str,
    model: str | None,
    max_turns: int | None,
    max_budget_usd: float | None,
) -> list[str]:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(AGENT_OUTPUT_SCHEMA),
        "--permission-mode",
        "bypassPermissions",
    ]
    if model:
        cmd.extend(["--model", model])
    if max_turns is not None:
        cmd.extend(["--max-turns", str(max_turns)])
    if max_budget_usd is not None:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])
    return cmd


def codex_command(
    *,
    help_text: str,
    schema_path: Path,
    output_path: Path,
    model: str | None,
    provider: str | None,
    capture_usage: bool,
) -> list[str]:
    cmd = ["codex", "exec"]
    has_config = supports_flag(help_text, "--config")

    if supports_flag(help_text, "--dangerously-bypass-approvals-and-sandbox"):
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        if supports_flag(help_text, "--sandbox"):
            cmd.extend(["--sandbox", "workspace-write"])
        elif has_config:
            cmd.extend(["--config", 'sandbox_mode="workspace-write"'])
        if has_config:
            cmd.extend(["--config", "sandbox_workspace_write.network_access=true"])
        if supports_flag(help_text, "--ask-for-approval"):
            cmd.extend(["--ask-for-approval", "never"])
        elif has_config:
            cmd.extend(["--config", 'approval_policy="never"'])

    if provider:
        if not has_config:
            raise RuntimeError(
                "Codex provider selection requires a Codex CLI that supports "
                "`codex exec --config`. Update Codex CLI and retry."
            )
        append_provider_config(cmd, provider)

    cmd.extend(
        [
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
    )
    if model:
        cmd.extend(["--model", model])
    if capture_usage and supports_flag(help_text, "--json"):
        cmd.append("--json")
    if has_config:
        cmd.extend(["--config", 'model_reasoning_summary="detailed"'])
    return cmd


def append_provider_config(cmd: list[str], provider: str) -> None:
    resolved = normalize_provider(provider)
    cmd.extend(["--config", config_arg("model_provider", resolved)])
    for key, value in openrouter_config(resolved):
        cmd.extend(["--config", config_arg(key, value)])


def normalize_provider(provider: str) -> str:
    lowered = provider.lower()
    if lowered in {"openai", "openrouter"}:
        return lowered
    return provider


def openrouter_config(provider: str) -> list[tuple[str, str]]:
    if provider != "openrouter":
        return []
    return [
        ("model_providers.openrouter.name", "OpenRouter"),
        ("model_providers.openrouter.base_url", "https://openrouter.ai/api/v1"),
        ("model_providers.openrouter.env_key", "OPENROUTER_API_KEY"),
        ("model_providers.openrouter.wire_api", "responses"),
    ]


def config_arg(key: str, value: str) -> str:
    return f"{key}={json.dumps(value)}"


def usage_provider(provider: str | None) -> str:
    if not provider:
        return "openai"
    return normalize_provider(provider)


def supports_flag(help_text: str, flag: str) -> bool:
    return flag in help_text


def codex_env(provider: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if (
        provider is None or provider.lower() == "openai"
    ) and not env.get("CODEX_API_KEY") and env.get("OPENAI_API_KEY"):
        env["CODEX_API_KEY"] = env["OPENAI_API_KEY"]
    if (
        provider
        and provider.lower() == "openrouter"
        and not env.get("OPENROUTER_API_KEY")
    ):
        raise RuntimeError(
            "--provider openrouter requires OPENROUTER_API_KEY in the environment "
            "or the action's openrouter-api-key input."
        )
    return env


def require_codex() -> str:
    codex_path = shutil.which("codex")
    if codex_path is None:
        raise RuntimeError(
            "Codex CLI was not found on PATH.\n\n"
            "Install the current Codex CLI, then retry generation:\n"
            "  curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh\n\n"
            "If you are in GitHub Actions, leave the action's install-agent input set to true."
        )

    try:
        completed = subprocess.run(
            ["codex", "exec", "--help"],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Timed out while checking whether Codex CLI at {codex_path} supports `codex exec`."
        ) from exc

    help_text = f"{completed.stdout}\n{completed.stderr}"
    supports_exec = (
        completed.returncode == 0
        and "--output-schema" in help_text
        and "--output-last-message" in help_text
    )
    if supports_exec:
        return help_text

    raise RuntimeError(
        "The installed Codex CLI does not support the non-interactive command required by LLM Fuzz CI.\n\n"
        f"Detected binary: {codex_path}\n"
        f"`codex exec --help` exit code: {completed.returncode}\n\n"
        "This commonly happens when an older npm-era `@openai/codex` binary appears first on PATH.\n"
        "Install or update the current Codex CLI, then open a new shell or make sure the new binary is first on PATH:\n"
        "  curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh\n\n"
        "Relevant help output:\n"
        f"{clip(help_text.strip() or '(empty)', 4000)}"
    )


def describe_failure(
    tool_name: str,
    cmd: list[str],
    completed: subprocess.CompletedProcess[str],
) -> str:
    output = f"{completed.stdout}\n{completed.stderr}"
    diagnosis = diagnose(output)
    diagnosis_block = f"\nlikely cause: {diagnosis}\n" if diagnosis else ""
    return (
        f"{tool_name} generation failed\n"
        f"exit code: {completed.returncode}\n"
        f"command: {printable_command(cmd)}\n\n"
        f"{diagnosis_block}"
        f"stdout excerpt:\n{important_lines(completed.stdout)}\n\n"
        f"stderr excerpt:\n{important_lines(completed.stderr)}"
    )


def printable_command(cmd: list[str]) -> str:
    redacted: list[str] = []
    for part in cmd:
        if "\n" in part:
            redacted.append("<prompt>")
        else:
            redacted.append(part)
    return " ".join(redacted)


def clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... truncated ..."


def diagnose(output: str) -> str | None:
    lowered = output.lower()
    if "no credits remaining" in lowered:
        return (
            "the OpenAI API key reached the API, but its project or organization "
            "has no remaining credits; add billing credits or use a funded key"
        )
    if "invalid_json_schema" in lowered or "invalid schema for response_format" in lowered:
        return "the agent output JSON Schema is not accepted by the model provider"
    if "not scoped to a workspace" in lowered:
        return (
            "the Anthropic API key belongs to the organization rather than a "
            "workspace; use a key created inside a workspace, or set "
            "ANTHROPIC_CUSTOM_HEADERS to 'anthropic-workspace-id: <id>'"
        )
    if "flagged for possible cybersecurity risk" in lowered:
        return (
            "the model provider refused the request under its cybersecurity "
            "policy; switch to --agent claude, or apply for the provider's "
            "authorized security-work program"
        )
    if "not inside a trusted directory" in lowered or "trusted directory" in lowered:
        return (
            "Codex refuses to run outside a trusted directory; run this from a "
            "git repository, or run `git init` first"
        )
    if "unexpected argument" in lowered:
        return "the installed agent CLI does not support one of the flags passed by LLM Fuzz CI"
    if "missing openai api key" in lowered or "api key" in lowered and "missing" in lowered:
        return "the selected agent did not receive an API key"
    return None


def important_lines(text: str, *, max_chars: int = 4000) -> str:
    redacted = redact_secrets(text).strip()
    if not redacted:
        return "(empty)"

    important_lines = [
        line
        for line in redacted.splitlines()
        if is_failure_line(line)
    ]
    if important_lines:
        return clip("\n".join(dedupe(important_lines)), max_chars)
    return clip(redacted, max_chars)


def redact_secrets(text: str) -> str:
    """Blank anything shaped like a provider or GitHub token."""
    return re.sub(r"\b(sk|gh[pousr]|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}", "<redacted>", text)


def is_failure_line(line: str) -> bool:
    lowered = line.lower()
    return (
        lowered.startswith("error")
        or " error=" in lowered
        or "error:" in lowered
        or "failed" in lowered
        or "invalid_" in lowered
        or "invalid schema" in lowered
        or "unauthorized" in lowered
        or "forbidden" in lowered
        or "no credits remaining" in lowered
        or "billing" in lowered
        or "unexpected argument" in lowered
        or "permission denied" in lowered
        or "operation not permitted" in lowered
    )


def dedupe(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        deduped.append(line)
    return deduped


def parse_agent_cases(
    output_text: str,
    target: FuzzTarget,
) -> tuple[list[FuzzCase], list[str]]:
    """Parse one agent reply into cases for the target that was requested.

    Generation is one target per agent call, so the target id is assigned here
    rather than trusted from the reply. When the marker declared `params`, the
    keys are held to it too: extra keys are dropped and an input missing one is
    discarded, so a wrong guess never reaches the test as a false finding.
    """
    raw = json.loads(strip_markdown(output_text))
    if "result" in raw and isinstance(raw["result"], str):
        raw = json.loads(strip_markdown(raw["result"]))
    if "cases" not in raw:
        raise ValueError("Agent output must contain a top-level 'cases' list")

    cases: list[FuzzCase] = []
    skipped: list[str] = []
    for index, item in enumerate(raw["cases"], start=1):
        try:
            case = FuzzCase.from_dict(dict(item), target_id=target.id)
            if target.params is not None:
                case = hold_to_params(case, target.params)
        except (ValueError, TypeError, KeyError) as exc:
            # One unusable input used to discard every other case the agent
            # produced for this target. Drop that one, keep the rest.
            skipped.append(f"{target.id} input {index}: {exc}")
            continue
        cases.append(case)

    if raw["cases"] and not cases:
        raise ValueError(
            f"No usable inputs in the agent reply for {target.id}:\n"
            + "\n".join(skipped)
        )
    return cases, skipped


def hold_to_params(case: FuzzCase, params: list[str]) -> FuzzCase:
    """Keep only the declared keys, and reject an input that is missing one."""
    missing = [name for name in params if name not in case.input]
    if missing:
        raise ValueError(f"missing declared param(s): {', '.join(missing)}")
    trimmed = {name: case.input[name] for name in params}
    if trimmed == case.input:
        return case
    return make_case(
        target_id=case.target_id, input_value=trimmed, rationale=case.rationale
    )


def strip_markdown(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped
