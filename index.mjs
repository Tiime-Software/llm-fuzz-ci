// Vitest side of LLM Fuzz CI.
//
// One helper, `fuzzTest`, standing where `@pytest.mark.llm_fuzz` stands in
// Python. It has three jobs, chosen by the environment the CLI sets up:
//
//   collect  — describe the target and run nothing
//   run      — turn every saved input into a test case
//   normally — skip, so a plain `vitest run` stays green and quiet
//
// It writes one file per record rather than appending to a shared one: vitest
// runs test files in parallel workers, and interleaved appends lose data.

import { randomUUID } from "node:crypto";
import { mkdirSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "vitest";

const COLLECT_DIR = process.env.LLM_FUZZ_COLLECT_DIR;
// Defaulted, so a plain `vitest run` records its results too. The CLI only
// overrides it to keep a run it drives out of the working tree.
const RESULT_DIR =
  process.env.LLM_FUZZ_RESULT_DIR ?? ".llm-fuzz/reports/vitest-results";
const CORPUS_DIR = process.env.LLM_FUZZ_CORPUS_DIR ?? ".llm-fuzz/cases";
const REQUIRE_CASES = process.env.LLM_FUZZ_REQUIRE_CASES === "1";

/** Mirrors sanitize_target_id in schema.py; the two must agree on filenames. */
export function sanitizeTargetId(targetId) {
  const safe = String(targetId)
    .replace(/[^A-Za-z0-9_.-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return safe || "target";
}

/** The test file that called us, as a repo-relative path. */
function callerFile() {
  const original = Error.prepareStackTrace;
  Error.prepareStackTrace = (_, frames) => frames;
  const frames = new Error().stack;
  Error.prepareStackTrace = original;

  const here = fileURLToPath(import.meta.url);
  for (const frame of frames ?? []) {
    let name = frame.getFileName?.();
    if (!name) continue;
    if (name.startsWith("file://")) name = fileURLToPath(name);
    if (name !== here && !name.includes("/node_modules/")) {
      return relative(process.cwd(), name);
    }
  }
  return "unknown";
}

function emit(directory, payload, name = randomUUID()) {
  mkdirSync(directory, { recursive: true });
  writeFileSync(join(directory, `${name}.json`), JSON.stringify(payload));
}

function loadCases(targetId) {
  const path = join(CORPUS_DIR, `${sanitizeTargetId(targetId)}.jsonl`);
  if (!existsSync(path)) return null; // never generated for
  return readFileSync(path, "utf8")
    .split("\n")
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line));
}

function record(entry, name) {
  // Named after the case rather than at random: running the suite twice has to
  // overwrite the first run's verdict, not leave both for the report to pick
  // between.
  emit(RESULT_DIR, entry, name);
}

/**
 * Mark a test as a fuzz target.
 *
 * @param {string} name        Test name; with the file path it identifies the target.
 * @param {{budgetUsd?: number, params?: string[], description?: string}} options
 * @param {(input: any) => unknown | Promise<unknown>} body  Receives one saved input.
 */
export function fuzzTest(name, options, body) {
  if (typeof options === "function") {
    body = options;
    options = {};
  }
  const sourceFile = callerFile();
  const targetId = `${sourceFile}::${name}`;

  if (COLLECT_DIR) {
    emit(COLLECT_DIR, {
      id: targetId,
      target: `vitest::${targetId}`,
      budget_usd: options.budgetUsd ?? null,
      params: options.params ?? null,
      description: options.description ?? null,
      language: "javascript",
      test_nodeid: targetId,
      source_file: sourceFile,
    });
    test.skip(name, () => {});
    return;
  }

  const cases = loadCases(targetId);

  if (cases === null) {
    if (!REQUIRE_CASES) return test.skip(`${name} [no-inputs]`, () => {});
    return test(`${name} [no-inputs]`, () => {
      record({
        nodeid: `${targetId} [no-inputs]`,
        outcome: "failed",
        duration: 0,
        target_id: targetId,
        case_id: `${targetId}::no-inputs`,
        input: null,
        rationale: "The generator produced no input for this test.",
        failure: `No saved inputs for '${targetId}'`,
      }, `${sanitizeTargetId(targetId)}-no-inputs`);
      throw new Error(`No saved inputs for '${targetId}'`);
    });
  }

  if (cases.length === 0) {
    // The agent looked at this target and found nothing worth sending.
    return test.skip(`${name} [no-weakness-found]`, () => {});
  }

  cases.forEach((saved, index) => {
    // No case_id here on purpose. It is a hash of the input that the Python
    // side already knows how to take, and two implementations of one hash
    // drift apart silently.
    const slot = `${sanitizeTargetId(targetId)}-${index}`;
    test(`${name} [${index + 1}/${cases.length}]`, async () => {
      const started = performance.now();
      const entry = {
        nodeid: `${targetId} [${index + 1}]`,
        target_id: targetId,
        input: saved.input,
        rationale: saved.rationale ?? "",
      };
      try {
        await body(saved.input);
      } catch (error) {
        record({
          ...entry,
          outcome: "failed",
          duration: (performance.now() - started) / 1000,
          failure: String(error?.stack ?? error),
        }, slot);
        throw error;
      }
      record({
        ...entry,
        outcome: "passed",
        duration: (performance.now() - started) / 1000,
      }, slot);
    });
  });
}

export default fuzzTest;
