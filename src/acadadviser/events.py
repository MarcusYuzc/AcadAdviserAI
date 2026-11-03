"""Reduce internal agent events to what a visitor's browser needs.

An allowlist: anything unrecognised, including tool output, never leaves the server.
"""
from __future__ import annotations

from typing import Any

_STAGE_BY_TOOL = {"get_student": "profile", "get_matcher_slate": "profile", "submit_slate": "finalizing"}
_FAILURES = {"psa.error", "agent.session.turn.failed", "agent.session.failed"}


def translate(event: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(event.get("type") or "")
    if kind == "agent.session.turn.output_text.delta" and event.get("delta"):
        return {"type": "note_delta", "delta": str(event["delta"])}
    if kind == "agent.session.turn.output_text.done":
        return {"type": "turn_done", "text": str(event.get("text") or "")}
    if kind == "psa.tool.start":
        stage = _STAGE_BY_TOOL.get(str(event.get("name")))
        return {"type": "stage", "stage": stage} if stage else None
    if "web_search" in kind:
        return {"type": "stage", "stage": "searching"}
    if kind == "psa.run.ready":
        return {
            "type": "list",
            "run_id": event.get("run_id"),
            "items": [public_item(item) for item in event.get("items") or []],
        }
    if kind == "psa.warn":
        return {"type": "failed", "message": "The search stopped early. You can ask it to continue."}
    if kind in _FAILURES:
        return {"type": "failed", "message": "The search could not finish. Please try again shortly."}
    return None


_ITEM_FIELDS = (
    "web_id", "university_name", "display_name", "degree_level", "canonical_field",
    "official_url", "why", "bucket", "warnings",
)


def public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in _ITEM_FIELDS}
