from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .schema import utc_now


@dataclass
class LLMUsage:
    provider: str
    model: str | None = None
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    # Dollars spent, when known: the agent's own figure (Claude Code reports
    # `total_cost_usd`) or an estimate from the model prices (Codex).
    cost_usd: float | None = None

    def add(self, other: "LLMUsage") -> None:
        if other.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + other.cost_usd
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_output_tokens += other.reasoning_output_tokens
        self.total_tokens += other.total_tokens

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data["cost_usd"] is None:
            del data["cost_usd"]
        return data


def format_usage_summary(usage: LLMUsage | None) -> str:
    if usage is None:
        return "LLM token usage: unavailable because the agent did not emit usage metadata."

    lines = [
        "LLM token usage:",
        f"  provider: {usage.provider}",
    ]
    if usage.model:
        lines.append(f"  model: {usage.model}")
    lines.extend(
        [
            f"  input tokens: {usage.input_tokens}",
            f"  cached input tokens: {usage.cached_input_tokens}",
            f"  output tokens: {usage.output_tokens}",
            f"  reasoning output tokens: {usage.reasoning_output_tokens}",
            f"  total tokens: {usage.total_tokens}",
        ]
    )
    if usage.cost_usd is not None:
        lines.append(f"  cost: ${usage.cost_usd:.2f}")
    return "\n".join(lines)


def write_usage_report(
    path: str | Path,
    *,
    agent: str,
    model: str | None,
    provider: str | None,
    target_count: int,
    case_count: int,
    usage: LLMUsage | None,
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "llm-fuzz.usage.v1",
        "generated_at": utc_now(),
        "agent": agent,
        "provider": provider,
        "model": model,
        "target_count": target_count,
        "case_count": case_count,
        "usage": usage.to_dict() if usage else None,
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return output


def extract_usage_from_json_events(
    text: str,
    *,
    provider: str,
    model: str | None,
) -> LLMUsage | None:
    """Total the tokens an agent reported, without counting any of them twice.

    Codex streams a cumulative `total_token_usage` per event; the last one is
    the whole run. Claude returns one result whose `usage` is a running total
    and may appear again nested under `modelUsage`. So: prefer the cumulative
    figure, else take one usage object per event and add those.
    """
    events = json_objects(text)

    running = [found for event in events for found in cumulative_usage(event)]
    if running:
        usage = usage_from_dict(running[-1], provider=provider, model=model)
        if usage is not None:
            usage.cost_usd = sum_costs(reported_cost(event) for event in events)
        return usage

    total: LLMUsage | None = None
    for event in events:
        data = first_usage(event)
        item = usage_from_dict(data, provider=provider, model=model) if data else None
        if item is None:
            continue
        item.cost_usd = reported_cost(event)
        if total is None:
            total = item
        else:
            total.add(item)

    if total and total.total_tokens == 0:
        total.total_tokens = total.input_tokens + total.output_tokens
    return total


def reported_cost(event: Any) -> float | None:
    """The dollars an agent says one run cost (Claude Code: `total_cost_usd`)."""
    if isinstance(event, dict):
        value = event.get("total_cost_usd")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def sum_costs(costs: Any) -> float | None:
    known = [cost for cost in costs if cost is not None]
    return sum(known) if known else None


def estimate_cost(usage: LLMUsage, price_input: float, price_output: float) -> float:
    """Dollars from the model prices (USD per million tokens).

    Cached input is billed lower than this assumes, so the figure is an upper
    bound.
    """
    return (usage.input_tokens * price_input + usage.output_tokens * price_output) / 1_000_000


def cumulative_usage(value: Any) -> list[dict[str, Any]]:
    """Every running total an agent reported, in the order it reported them."""
    if isinstance(value, dict):
        found = []
        running = value.get("total_token_usage")
        if isinstance(running, dict):
            found.append(running)
        for child in value.values():
            found.extend(cumulative_usage(child))
        return found
    if isinstance(value, list):
        return [found for child in value for found in cumulative_usage(child)]
    return []


def first_usage(value: Any) -> dict[str, Any] | None:
    """The one usage object for this event, preferring the outermost."""
    if isinstance(value, dict):
        data = value.get("usage")
        if isinstance(data, dict):
            return data
        for child in value.values():
            found = first_usage(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = first_usage(child)
            if found is not None:
                return found
    return None


def usage_from_dict(data: dict[str, Any], *, provider: str, model: str | None) -> LLMUsage | None:
    input_tokens = int_value(data, "input_tokens")
    output_tokens = int_value(data, "output_tokens")
    cached_input_tokens = int_value(data, "cached_input_tokens")
    reasoning_output_tokens = int_value(data, "reasoning_output_tokens")
    total_tokens = int_value(data, "total_tokens")

    # Claude-style cache fields are common in JSON summaries. Treat cache reads
    # as cached input; cache creation is still ordinary input token usage.
    cached_input_tokens += int_value(data, "cache_read_input_tokens")
    input_tokens += int_value(data, "cache_creation_input_tokens")

    details = data.get("input_tokens_details")
    if isinstance(details, dict):
        cached_input_tokens += int_value(details, "cached_tokens")

    output_details = data.get("output_tokens_details")
    if isinstance(output_details, dict):
        reasoning_output_tokens += int_value(output_details, "reasoning_tokens")

    if not any((input_tokens, output_tokens, cached_input_tokens, reasoning_output_tokens, total_tokens)):
        return None

    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens

    return LLMUsage(
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        total_tokens=total_tokens,
    )


def json_objects(text: str) -> list[Any]:
    stripped = text.strip()
    if not stripped:
        return []

    try:
        parsed = json.loads(stripped)
        return [parsed]
    except json.JSONDecodeError:
        pass

    objects: list[Any] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            objects.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return objects


def int_value(data: dict[str, Any], key: str) -> int:
    value = data.get(key, 0)
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
