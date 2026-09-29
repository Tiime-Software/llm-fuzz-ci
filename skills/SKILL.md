---
name: llm-fuzz-harness
description: Write or review LLM Fuzz CI harness tests — the marked pytest or vitest tests whose assertions judge agent-generated inputs. Use when asked to add LLM Fuzz coverage, pick functions worth fuzzing, write the invariants a marked test asserts, or prepare a repository for the LLM Fuzz CI action. Not for generating fuzz inputs; the action does that.
metadata:
  short-description: "Write the marked tests whose assertions judge LLM Fuzz CI inputs."
---

# LLM Fuzz Harness

A marked test does two things: it names a function worth attacking, and it says
what must remain true whatever that function is handed. The action's agent
writes the inputs. The assertion is yours, and it is the only thing that decides
pass or fail.

## Choosing what to fuzz

Look for code that is handed something it did not construct: request and header
parsing, URL and path handling, redirect validation, authorisation decisions,
template and prompt construction, SQL and shell building, deserialisation,
HTML rendering, numeric boundaries, file access.

Prefer functions where you can state the invariant in one sentence. If you
cannot say what must remain true, the test will not be able to either.

## Writing the test

Python:

```python
@pytest.mark.llm_fuzz(budget_usd=0.5, params=["url"])
def test_only_relative_paths_are_accepted(llm_fuzz_case):
    assert is_safe_redirect(llm_fuzz_case.input["url"]) is False or \
        llm_fuzz_case.input["url"].startswith("/")
```

JavaScript:

```js
import { fuzzTest } from "llm-fuzz-ci";

fuzzTest("only relative paths are accepted", { budgetUsd: 0.5, params: ["url"] }, (input) => {
  expect(isSafeRedirect(input.url) && !input.url.startsWith("/")).toBe(false);
});
```

- `params` limits generation to those keys. Declare it whenever only part of
  the input is attacker-controlled; without it the agent infers the whole shape.
- `budget_usd` / `budgetUsd` caps spend, and is enforced on Claude Code only.
- The target id is the file path and test name, so renaming a test starts it
  over with a fresh corpus.

## Ground the invariant in real values

An assertion that compares against a constant the agent can read is one the
agent can attack. Import the real allowlist, the real delimiter, the real
header. An invariant written against an invented example domain is one the
agent will satisfy without ever reaching the interesting branch.

## Assertions worth writing

- The classification a security decision returns agrees with a plain restatement
  of what that decision is supposed to mean.
- Output stays inside an allowlisted domain, directory, role, or schema.
- Generated SQL, shell, paths, URLs, HTML or prompts keep their escaping and
  their delimiters: a field's own text cannot add a section or close a quote.
- Structure is not forgeable: a header, separator or placeholder appears as
  often after untrusted input as before it.
- Secrets, stack traces, internal ids and system prompts stay out of output.

Avoid:

- Checking only for `not None` or a type.
- Keyword checks unrelated to a stated policy.
- Catching every exception and calling it a pass.
- Calling real external services, unless the project already isolates them.
- Executing anything the agent wrote. It produces data, never code.

## Final check

Report which tests you marked and what each assertion protects. For each one,
answer in a sentence: *which input would make this fail?* If you cannot name
one, the assertion is too weak to be worth a run.
