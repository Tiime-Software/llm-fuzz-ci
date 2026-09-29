from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


TARGET_SCHEMA_VERSION = "llm-fuzz.targets.v1"
REPORT_SCHEMA_VERSION = "llm-fuzz.report.v1"
DEFAULT_CASE_RATIONALE = "Generated adversarial input."


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def sanitize_target_id(target_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", target_id).strip("-")
    return safe or "target"


def stable_case_id(target_id: str, input_value: dict[str, Any]) -> str:
    payload = json.dumps(
        {"target_id": target_id, "input": input_value},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"{sanitize_target_id(target_id)}-{digest}"


@dataclass
class FuzzTarget:
    id: str
    target: str
    budget_usd: float | None = None
    params: list[str] | None = None
    description: str | None = None
    language: str = "python"
    test_nodeid: str | None = None
    source_file: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FuzzTarget":
        return cls(
            id=str(data.get("id") or data.get("name") or data["target"]),
            target=str(data["target"]),
            budget_usd=positive_float(data, "budget_usd"),
            params=name_list(data, "params"),
            description=data.get("description"),
            language=str(data.get("language", "python")),
            test_nodeid=data.get("test_nodeid"),
            source_file=data.get("source_file"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def name_list(data: dict[str, Any], key: str) -> list[str] | None:
    """Read a list of parameter names, or None when the caller declared none."""
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f"Fuzz target {key!r} must be a list of names")
    names = [str(item) for item in value]
    if not names or any(not name.strip() for name in names):
        raise ValueError(f"Fuzz target {key!r} must be a list of non-empty names")
    return names


def positive_float(data: dict[str, Any], key: str) -> float | None:
    if data.get(key) is None:
        return None
    try:
        value = float(data[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Fuzz target {key!r} must be a number") from exc
    if value <= 0:
        raise ValueError(f"Fuzz target {key!r} must be greater than 0")
    return value


@dataclass
class FuzzCase:
    target_id: str
    input: dict[str, Any]
    rationale: str = DEFAULT_CASE_RATIONALE
    id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, target_id: str | None = None) -> "FuzzCase":
        """Build a case from stored JSONL or from one agent reply.

        `target_id` is supplied by the caller for agent output: generation runs
        one target at a time, so the agent never needs to name the target, and
        cannot mis-name it.
        """
        resolved = str(target_id if target_id is not None else data["target_id"])
        input_value = data.get("input", {})
        if "input_json" in data and "input" not in data:
            input_value = parse_input_json(str(data["input_json"]))
        if not isinstance(input_value, dict):
            raise ValueError(f"Fuzz case input must be an object: {data!r}")
        rationale = str(data.get("rationale") or DEFAULT_CASE_RATIONALE)
        return cls(
            target_id=resolved,
            input=input_value,
            rationale=rationale,
            id=str(data.get("id") or stable_case_id(resolved, input_value)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "input": self.input,
            "rationale": self.rationale,
        }


def parse_input_json(raw: str) -> dict[str, Any]:
    """Decode the JSON object an agent hand-encoded inside a string.

    Structured output guarantees `input_json` is a string, never that its
    contents parse: the agent has to escape a JSON document inside a JSON
    string, and payloads full of quotes, backslashes, and newlines are exactly
    where that goes wrong. Repair the two malformations that actually show up,
    then give a message that names the offending text.
    """
    text = strip_code_fence(raw.strip())
    for candidate in (text, drop_trailing_commas(text)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last = exc
    raise ValueError(
        f"input_json is not valid JSON ({last.msg} at column {last.colno}): "
        f"{clip_for_error(text)}"
    ) from last


def strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def drop_trailing_commas(text: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", text)


def clip_for_error(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[:limit] + "... (truncated)"


def make_case(
    *,
    target_id: str,
    input_value: dict[str, Any],
    rationale: str = DEFAULT_CASE_RATIONALE,
) -> FuzzCase:
    return FuzzCase(
        target_id=target_id,
        input=input_value,
        rationale=rationale,
        id=stable_case_id(target_id, input_value),
    )


def load_targets(path: str | Path) -> list[FuzzTarget]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    target_data = raw.get("targets", raw) if isinstance(raw, dict) else raw
    if not isinstance(target_data, list):
        raise ValueError("Target file must contain a list or a {'targets': [...]} object")
    return [FuzzTarget.from_dict(item) for item in target_data]


def write_targets(path: str | Path, targets: Iterable[FuzzTarget]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": TARGET_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "targets": [target.to_dict() for target in targets],
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return output


def case_file(corpus_dir: str | Path, target_id: str) -> Path:
    return Path(corpus_dir) / f"{sanitize_target_id(target_id)}.jsonl"


def load_cases(corpus_dir: str | Path, target_id: str | None = None) -> list[FuzzCase]:
    root = Path(corpus_dir)
    if not root.exists():
        return []

    files = [case_file(root, target_id)] if target_id else sorted(root.glob("*.jsonl"))
    cases: list[FuzzCase] = []
    for path in files:
        if not path.exists():
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                cases.append(FuzzCase.from_dict(json.loads(stripped)))
            except Exception as exc:
                raise ValueError(f"Invalid fuzz case in {path}:{line_number}: {exc}") from exc
    return cases


def write_cases(
    corpus_dir: str | Path,
    cases: Iterable[FuzzCase],
    *,
    merge: bool = False,
) -> list[Path]:
    root = Path(corpus_dir)
    root.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[FuzzCase]] = {}
    for case in cases:
        grouped.setdefault(case.target_id, []).append(case)

    written: list[Path] = []
    for target_id, new_cases in grouped.items():
        path = case_file(root, target_id)
        merged: dict[str, FuzzCase] = {}
        if merge:
            for existing in load_cases(root, target_id):
                merged[dedupe_key(existing)] = existing
        for case in new_cases:
            merged[dedupe_key(case)] = case
        serialized = "\n".join(json.dumps(case.to_dict()) for case in merged.values())
        path.write_text(serialized + ("\n" if serialized else ""), encoding="utf-8")
        written.append(path)
    return written


def dedupe_key(case: FuzzCase) -> str:
    return stable_case_id(case.target_id, case.input)


AGENT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "cases": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "input_json": {
                        "type": "string",
                        "description": "A JSON-encoded object containing the generated input parameters.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "One sentence on what weakness this input probes.",
                    },
                },
                "required": [
                    "input_json",
                    "rationale",
                ],
            },
        }
    },
    "required": ["cases"],
}
