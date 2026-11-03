from __future__ import annotations

from acadadviser.events import translate


def test_unknown_and_tool_output_events_are_dropped():
    assert translate({"type": "psa.tool.done", "name": "get_student", "output": "x"}) is None
    assert translate({"type": "something.new", "secret": "x"}) is None
    assert translate({"type": "psa.tool.start", "name": "other"}) is None


def test_failure_messages_are_replaced():
    assert "boom" not in str(translate({"type": "psa.error", "message": "boom"}))
    assert "boom" not in str(translate({"type": "psa.warn", "message": "boom"}))
