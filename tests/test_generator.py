import json
import subprocess
from pathlib import Path

import pytest

from llm_fuzz_ci import generator
from llm_fuzz_ci.generator import generate_cases
from llm_fuzz_ci.schema import FuzzTarget, make_case
from llm_fuzz_ci.usage import LLMUsage


def test_parse_agent_cases_accepts_structured_output():
    cases, skipped = generator.parse_agent_cases(
        """
        {
          "cases": [
            {
              "input_json": "{\\"x\\": 1, \\"y\\": 0}",
              "rationale": "Division by zero"
            }
          ]
        }
        """,
        FuzzTarget(id="tests/test_app.py::test_divide", target="pytest::t"),
    )

    assert skipped == []
    assert len(cases) == 1
    assert cases[0].target_id == "tests/test_app.py::test_divide"
    assert cases[0].input == {"x": 1, "y": 0}
    assert cases[0].rationale == "Division by zero"


def test_agent_prompt_is_rendered_from_template(tmp_path):
    prompt = generator.build_prompt(
        FuzzTarget(id="divide", target="pytest::tests/test_app.py::test_divide", budget_usd=0.25),
        tmp_path,
    )

    assert "{{" not in prompt
    assert str(tmp_path) in prompt
    assert '"id": "divide"' in prompt
    assert '"budget_usd": 0.25' in prompt
    assert "Every case has input_json and rationale." in prompt
    # quantity follows from what the agent finds, so no cap may leak back in
    assert "at most" not in prompt
    assert "return an empty cases list" in prompt
    # several inputs may probe one weakness when each can fail where others pass
    assert "Several inputs for one weakness are right" in prompt
    assert "There is no target number." in prompt



def test_claude_generation_uses_required_target_budget_per_target(monkeypatch):
    captured = []

    def fake_claude(
        target, repo_root, *, model, max_turns, timeout_seconds, capture_usage, trace_dir
    ):
        captured.append((target.id, target.budget_usd))
        return generator.Generated(
            [],
            LLMUsage(provider="anthropic", input_tokens=10, output_tokens=5, total_tokens=15),
        )

    monkeypatch.setattr(generator, "generate_with_claude", fake_claude)

    result = generate_cases(
        [
            FuzzTarget(id="checkout", target="app:checkout", budget_usd=0.25),
            FuzzTarget(id="redirect", target="app:redirect", budget_usd=0.10),
        ],
        agent="claude",
        repo_root=".",
        capture_usage=True,
    )

    assert captured == [("checkout", 0.25), ("redirect", 0.10)]
    assert result.usage is not None
    assert result.usage.total_tokens == 30


def test_global_max_budget_overrides_marker_budget_per_target(monkeypatch):
    captured = []

    def fake_claude(
        target, repo_root, *, model, max_turns, timeout_seconds, capture_usage, trace_dir
    ):
        captured.append((target.id, target.budget_usd))
        return generator.Generated([])

    monkeypatch.setattr(generator, "generate_with_claude", fake_claude)

    generate_cases(
        [
            FuzzTarget(id="checkout", target="app:checkout", budget_usd=0.25),
            FuzzTarget(id="redirect", target="app:redirect", budget_usd=0.10),
        ],
        agent="claude",
        repo_root=".",
        max_budget_usd=0.50,
    )

    assert captured == [("checkout", 0.50), ("redirect", 0.50)]


def test_codex_preflight_rejects_old_cli(monkeypatch):
    monkeypatch.setattr(generator.shutil, "which", lambda name: "/fake/bin/codex")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=2,
            stdout="Usage\n  $ codex [options] <prompt>",
            stderr="",
        )

    monkeypatch.setattr(generator.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="does not support the non-interactive command"):
        generator.require_codex()


def test_codex_command_uses_config_approval_when_flag_is_absent(tmp_path):
    help_text = """
    Usage: codex exec [OPTIONS] [PROMPT]
      -s, --sandbox <SANDBOX_MODE>
          --config <key=value>
          --output-schema <FILE>
      -o, --output-last-message <FILE>
          --json
    """

    cmd = generator.codex_command(
        help_text=help_text,
        schema_path=tmp_path / "schema.json",
        output_path=tmp_path / "output.json",
        model="gpt-5.6-terra",
        provider=None,
        capture_usage=True,
    )

    assert "--ask-for-approval" not in cmd
    assert cmd[:4] == ["codex", "exec", "--sandbox", "workspace-write"]
    assert "sandbox_workspace_write.network_access=true" in cmd
    assert "--config" in cmd
    assert 'approval_policy="never"' in cmd
    assert "--output-schema" in cmd
    assert "--output-last-message" in cmd
    assert "--json" in cmd
    model_index = cmd.index("--model")
    assert cmd[model_index + 1] == "gpt-5.6-terra"


def test_codex_command_keeps_ask_for_approval_when_flag_exists(tmp_path):
    help_text = """
    Usage: codex exec [OPTIONS] [PROMPT]
      --sandbox <SANDBOX_MODE>
      --ask-for-approval <MODE>
      --config <key=value>
      --output-schema <FILE>
      --output-last-message <FILE>
    """

    cmd = generator.codex_command(
        help_text=help_text,
        schema_path=tmp_path / "schema.json",
        output_path=tmp_path / "output.json",
        model=None,
        provider=None,
        capture_usage=False,
    )

    assert "--ask-for-approval" in cmd
    assert "never" in cmd
    assert 'approval_policy="never"' not in cmd
    assert "--json" not in cmd


def test_claude_command_leaves_the_agent_unrestricted():
    cmd = generator.claude_command(
        prompt="p", model="sonnet", max_turns=None, max_budget_usd=None
    )

    assert "--tools" not in cmd
    assert "--allowed-tools" not in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"



def test_codex_command_can_select_openrouter_provider(tmp_path):
    help_text = """
    Usage: codex exec [OPTIONS] [PROMPT]
      --sandbox <SANDBOX_MODE>
      --config <key=value>
      --output-schema <FILE>
      --output-last-message <FILE>
    """

    cmd = generator.codex_command(
        help_text=help_text,
        schema_path=tmp_path / "schema.json",
        output_path=tmp_path / "output.json",
        model="openrouter/model-slug",
        provider="openrouter",
        capture_usage=False,
    )

    assert "--config" in cmd
    assert 'model_provider="openrouter"' in cmd
    assert 'model_providers.openrouter.base_url="https://openrouter.ai/api/v1"' in cmd
    assert 'model_providers.openrouter.env_key="OPENROUTER_API_KEY"' in cmd
    assert 'model_providers.openrouter.wire_api="responses"' in cmd


def test_codex_openrouter_shortcut_is_case_insensitive(tmp_path):
    help_text = """
    Usage: codex exec [OPTIONS] [PROMPT]
      --config <key=value>
      --output-schema <FILE>
      --output-last-message <FILE>
    """

    cmd = generator.codex_command(
        help_text=help_text,
        schema_path=tmp_path / "schema.json",
        output_path=tmp_path / "output.json",
        model=None,
        provider="OpenRouter",
        capture_usage=False,
    )

    assert 'model_provider="openrouter"' in cmd


def test_non_codex_provider_is_rejected():
    with pytest.raises(ValueError, match="applies only to --agent codex"):
        generate_cases(
            [FuzzTarget(id="divide", target="app:divide", budget_usd=0.25)],
            agent="claude",
            repo_root=".",
            provider="openrouter",
        )


def test_openrouter_provider_requires_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        generator.codex_env("openrouter")


def test_process_failure_summarizes_billing_error_and_redacts_key():
    completed = subprocess.CompletedProcess(
        args=["codex"],
        returncode=1,
        stdout="",
        stderr=(
            "user\n"
            "very long prompt body\n"
            "ERROR: stream disconnected before completion: You have no credits remaining. "
            "Add credits to continue using the API. key sk-proj-secret\n"
        ),
    )

    message = generator.describe_failure("Codex", ["codex", "exec"], completed)

    assert "likely cause:" in message
    assert "no remaining credits" in message
    assert "sk-proj-secret" not in message
    assert "<redacted>" in message
    assert "very long prompt body" not in message


def test_process_failure_explains_a_workspaceless_api_key():
    completed = subprocess.CompletedProcess(
        args=["claude"],
        returncode=1,
        stdout=(
            '{"is_error":true,"api_error_status":400,"result":"API Error: 400 This API key '
            "is not scoped to a workspace, so this request must include the "
            'anthropic-workspace-id header with the ID of the workspace to use."}'
        ),
        stderr="",
    )

    message = generator.describe_failure("Claude", ["claude"], completed)

    assert "likely cause:" in message
    assert "key created inside a workspace" in message


def test_process_failure_explains_a_provider_policy_refusal():
    completed = subprocess.CompletedProcess(
        args=["codex"],
        returncode=1,
        stdout=(
            '{"type":"turn.failed","error":{"message":"This content was flagged for '
            'possible cybersecurity risk."}}'
        ),
        stderr="",
    )

    message = generator.describe_failure("Codex", ["codex"], completed)

    assert "refused the request under its cybersecurity" in message
    assert "--agent claude" in message


def test_one_unparseable_case_does_not_discard_the_others():
    cases, skipped = generator.parse_agent_cases(
        json.dumps(
            {
                "cases": [
                    {"input_json": '{"a": 1}', "rationale": "fine"},
                    {"input_json": "{'b': 2}", "rationale": "single quotes"},
                    {"input_json": '{"c": 3,}', "rationale": "trailing comma"},
                ]
            }
        ),
        FuzzTarget(id="tests/test_app.py::test_x", target="pytest::t"),
    )

    assert [case.input for case in cases] == [{"a": 1}, {"c": 3}]
    assert len(skipped) == 1
    assert "input 2" in skipped[0]
    assert "not valid JSON" in skipped[0]


def test_an_entirely_unparseable_reply_still_fails_loudly():
    with pytest.raises(ValueError, match="No usable inputs"):
        generator.parse_agent_cases(
            json.dumps({"cases": [{"input_json": "{'a': 1}", "rationale": "bad"}]}),
            FuzzTarget(id="tests/test_app.py::test_x", target="pytest::t"),
        )


def test_one_failing_target_keeps_the_others(monkeypatch):
    """Targets are independent, and generation costs money before it fails."""
    def flaky(target, repo_root, **kwargs):
        if target.id == "boom":
            raise RuntimeError("Codex generation failed\nexit code: 1")
        return generator.Generated([make_case(target_id=target.id, input_value={"x": 1})])

    monkeypatch.setattr(generator, "generate_with_codex", flaky)

    result = generate_cases(
        [
            FuzzTarget(id="first", target="app:a", budget_usd=0.1),
            FuzzTarget(id="boom", target="app:b", budget_usd=0.1),
            FuzzTarget(id="last", target="app:c", budget_usd=0.1),
        ],
        agent="codex",
        repo_root=".",
    )

    assert [case.target_id for case in result.cases] == ["first", "last"]
    assert result.skipped == []
    assert list(result.failures) == ["boom"]
    assert "Codex generation failed" in result.failures["boom"]


def declared(params):
    return FuzzTarget(id="tests/test_app.py::test_x", target="pytest::t", params=params)


def test_declared_params_are_named_in_the_prompt(tmp_path):
    prompt = generator.build_prompt(declared(["amount", "currency"]), tmp_path)

    assert 'exactly these keys and no others: "amount", "currency"' in prompt
    assert "Infer the input_json keys" not in prompt


def test_without_params_the_agent_is_told_to_infer_the_keys(tmp_path):
    prompt = generator.build_prompt(declared(None), tmp_path)

    assert "Infer the input_json keys" in prompt
    assert "exactly these keys" not in prompt


def test_declared_params_drop_keys_the_agent_invented():
    reply = json.dumps(
        {"cases": [{"input_json": '{"amount": 5, "precision": 2}', "rationale": "extra"}]}
    )

    cases, skipped = generator.parse_agent_cases(reply, declared(["amount"]))

    assert [case.input for case in cases] == [{"amount": 5}]
    assert skipped == []


def test_an_input_missing_a_declared_param_is_discarded():
    reply = json.dumps(
        {
            "cases": [
                {"input_json": '{"amount": 1}', "rationale": "good"},
                {"input_json": '{"precision": 2}', "rationale": "no amount"},
            ]
        }
    )

    cases, skipped = generator.parse_agent_cases(reply, declared(["amount"]))

    assert [case.input for case in cases] == [{"amount": 1}]
    assert "missing declared param(s): amount" in skipped[0]


def test_nothing_is_enforced_when_the_marker_declares_no_params():
    reply = json.dumps(
        {"cases": [{"input_json": '{"anything": 1, "goes": 2}', "rationale": "free"}]}
    )

    cases, skipped = generator.parse_agent_cases(reply, declared(None))

    assert [case.input for case in cases] == [{"anything": 1, "goes": 2}]
    assert skipped == []


BYPASS = "--dangerously-bypass-approvals-and-sandbox"

HELP_WITH_BYPASS = f"""
Usage: codex exec [OPTIONS] [PROMPT]
  -s, --sandbox <SANDBOX_MODE>
      --config <key=value>
      {BYPASS}
      --output-schema <FILE>
  -o, --output-last-message <FILE>
"""

HELP_WITHOUT_BYPASS = """
Usage: codex exec [OPTIONS] [PROMPT]
  -s, --sandbox <SANDBOX_MODE>
      --config <key=value>
      --output-schema <FILE>
  -o, --output-last-message <FILE>
"""


def codex_argv(help_text):
    return generator.codex_command(
        help_text=help_text,
        schema_path=Path("s.json"),
        output_path=Path("o.json"),
        model=None,
        provider=None,
        capture_usage=False,
    )


def test_codex_runs_without_a_second_sandbox_when_the_cli_can():
    """The CI runner is the isolation. Codex sandboxing itself inside it has to
    stand up a kernel policy and, with the network allowed, a proxy for every
    command -- and when that fails the agent loses its shell without saying so,
    then invents the code it cannot read."""
    cmd = codex_argv(HELP_WITH_BYPASS)

    assert BYPASS in cmd
    assert "--sandbox" not in cmd
    assert not any("network_access" in part for part in cmd)


def test_codex_keeps_the_sandbox_when_the_cli_offers_no_alternative():
    cmd = codex_argv(HELP_WITHOUT_BYPASS)

    assert cmd[:4] == ["codex", "exec", "--sandbox", "workspace-write"]
    assert "sandbox_workspace_write.network_access=true" in cmd


def test_the_prompt_names_the_language_and_runner_of_the_target():
    js = generator.build_prompt(
        FuzzTarget(
            id="tests/a.test.mjs::rejects",
            target="vitest::tests/a.test.mjs::rejects",
            language="javascript",
        ),
        Path("/repo"),
    )

    assert "one piece of JavaScript code" in js
    assert "a marked vitest test" in js
    # a JavaScript author should never be told to read a pytest harness
    assert "pytest" not in js
    assert "{{" not in js


def test_a_python_target_still_reads_as_pytest(tmp_path):
    py = generator.build_prompt(
        FuzzTarget(id="tests/test_a.py::test_x", target="pytest::tests/test_a.py::test_x"),
        tmp_path,
    )

    assert "one piece of Python code" in py
    assert "a marked pytest test" in py


def test_the_prompt_refuses_an_answer_written_before_reading(tmp_path):
    """An early stub answer was a real failure mode, not a hypothetical one."""
    prompt = generator.build_prompt(
        FuzzTarget(id="t", target="pytest::t"), tmp_path
    )

    assert "Do not answer before you have read both." in prompt
