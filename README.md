# LLM Fuzz CI

[![Tests](https://github.com/NDV-tiime/llm-fuzz-ci/actions/workflows/tests.yml/badge.svg)](https://github.com/NDV-tiime/llm-fuzz-ci/actions/workflows/tests.yml)

Fuzz your Python or JavaScript code with a coding agent, in GitHub Actions.

Mark a test. The agent reads your code and writes adversarial inputs for it.

## Quick start

Mark the tests you want fuzzed. The agent fills the input with arguments for the call.

```python
import pytest

@pytest.mark.llm_fuzz(budget_usd=0.5)
def test_foo(llm_fuzz_case):
    result = foo(**llm_fuzz_case.input)
    assert "<script>" not in result
```

For vitest, `npm install --save-dev github:NDV-tiime/llm-fuzz-ci` and mark it the same way:

```js
import { expect } from "vitest";
import { fuzzTest } from "llm-fuzz-ci";

fuzzTest("foo escapes its input", { budgetUsd: 0.5 }, (input) => {
  expect(foo(input.value)).not.toContain("<script>");
});
```

Add `.github/workflows/llm-fuzz-ci.yml`:

```yaml
name: LLM Fuzz CI

on:
  workflow_dispatch:

jobs:
  llm-fuzz-ci:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      issues: write

    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"
      - run: pip install -e .          # your setup, however you do it

      - uses: NDV-tiime/llm-fuzz-ci@v1
        with:
          test-paths: tests
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}

      - run: pytest tests -m llm_fuzz
        continue-on-error: true

      # summary, issue, exit code
      - run: llm-fuzz-ci report --create-issue --hard-fail
        env:
          GITHUB_TOKEN: ${{ github.token }}

      - uses: actions/upload-artifact@v7
        if: always()
        with:
          name: llm-fuzz-ci
          include-hidden-files: true
          path: |
            .llm-fuzz
            !.llm-fuzz/reports/vitest-results
```

For a Node project:

```yaml
      - uses: actions/setup-node@v6
        with:
          node-version: "22"
      - run: npm ci

      - uses: NDV-tiime/llm-fuzz-ci@v1
        with:
          test-paths: tests
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}

      - run: npx vitest run .fuzz.
        continue-on-error: true
```

Run it from the Actions tab.

Set the job up the way you would for any other test run: dependencies in steps
before it, databases and queues in `services`, configuration in the job's `env`.

The test step needs no flags for the corpus: the plugin loads through its entry point and reads `.llm-fuzz/cases`. What it does need is a way to run only the marked tests, or an unrelated failure reddens a fuzz run. `-m llm_fuzz` does that for pytest. vitest has no markers, so narrow by filename — `npx vitest run .fuzz.` matches any path containing `.fuzz.`, which is why the examples are named `redirect.fuzz.test.mjs`. A dedicated directory works as well: `npx vitest run tests/fuzz`.

An annotated copy is in
[`templates/llm-fuzz-ci.yml`](templates/llm-fuzz-ci.yml).

## Marker options

| Argument | Description |
| --- | --- |
| `budget_usd` | Per-test spend limit. |
| `params` | Limit generation to these input keys. Without it the agent works out the whole signature from your harness. |

```python
@pytest.mark.llm_fuzz(budget_usd=0.5, params=["amount"])
def test_transfer(llm_fuzz_case):
    result = transfer(account_id="acct_1", amount=llm_fuzz_case.input["amount"])
    assert result.amount >= 0
```

## Configuration


| Input | Default | Description |
| --- | --- | --- |
| `test-paths` | `tests` | paths holding marked tests |
| `runner` | `auto` | `pytest`, `vitest`, or `auto` from the paths |
| `working-directory` | `.` | subdirectory to run in |
| `agent` | `codex` | `codex` or `claude` |
| `model` | | model for the agent; empty uses its default |
| `provider` | | Codex provider, for example `openrouter` |
| `openai-api-key` | | key for `codex` |
| `openrouter-api-key` | | key for `provider: openrouter` |
| `anthropic-api-key` | | key for `claude` |
| `max-budget-usd` | | override every marker budget |
| `timeout-seconds` | `600` | maximum generation time per target |
| `show-usage` | `false` | print the agent's token usage |

`llm-fuzz-ci report`, step 3:

| Flag | Description |
| --- | --- |
| `--create-issue` | open an issue when an input failed; needs `GITHUB_TOKEN` and `issues: write` |
| `--issue-assignees` | comma-separated logins; GitHub emails an assignee |
| `--issue-labels` | comma-separated labels |
| `--hard-fail` | exit non-zero when an input failed |

Both are off unless you pass them, so the same command works for a run you only
want to look at.

## Alerts

Every run writes a summary to the Actions run page: one row per marked test with
its outcome, each failing input in full with the assertion that fired.

The `llm-fuzz-ci` artifact holds the whole run:

| | |
| --- | --- |
| `cases/` | every input the agent wrote, as JSON Lines |
| `targets.json` | the marked tests it was pointed at |
| `reports/llm-fuzz-ci-report.md` | the same summary, unfolded |
| `reports/test-report.json` | one record per input, for processing |
| `reports/llm-usage.json` | tokens spent |
| `reports/agent-trace/` | per test, what the agent reasoned, ran, and saw |

All of the following are off unless you turn them on.

**An issue.** With `create-issue: true` every failing run opens one, titled with the number of failing inputs and linking back to the run. Needs `issues: write`.

**An email.** GitHub emails the assignee of an issue, use`issue-assignees: you` and `issue-labels`.

**A red build.** `hard-fail: true`, the default. The failure is the last thing the action does, so the summary, the artifact and the issue all land first. It fails the job, which skips the steps after it and any job that `needs:` it; jobs already running in parallel are not cancelled.

**Anything else.** The action outputs `failed-inputs`, so a step of your own can post to Slack, Teams, or a pager:

Set `hard-fail: false` when you do that, or the job dies before your step runs.

## Agents

| Agent | Key |
| --- | --- |
| `codex` (default) | `openai-api-key`, or `openrouter-api-key` with `provider: openrouter` |
| `claude` | `anthropic-api-key` |

OpenAI's safety classifier sometimes refuses this workload with `flagged for possible cybersecurity risk`. `agent: claude` is the quickest way past it; [Trusted Access for Cyber](https://chatgpt.com/cyber) is the durable one.

## Command line

The action wraps a CLI you can run locally.

```bash
pip install "git+https://github.com/NDV-tiime/llm-fuzz-ci.git@v1"
export CODEX_API_KEY=...

llm-fuzz-ci collect tests                                   # find marked tests
llm-fuzz-ci generate --dry-run                              # print the prompt, spend nothing
llm-fuzz-ci generate                                        # write .llm-fuzz/cases
llm-fuzz-ci test-fuzz-cases --require-cases -- tests -q     # run them
llm-fuzz-ci summary                                         # render the report
```

## Help writing the tests

Choosing what to fuzz and what to assert is the part that takes thought.
[`skills/SKILL.md`](skills/SKILL.md) is an agent skill for exactly that: it picks
out the functions worth fuzzing, writes the marked tests.

## License

MIT
