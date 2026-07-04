from __future__ import annotations
from datetime import date
from typing import Any


def resolve_status(rules: dict[str, Any], today: date) -> str:
    """Resolve the effective status of a legal instrument given today's date.

    rules format:
        {"default": "not_yet_in_force", "transitions": [{"on": "YYYY-MM-DD", "to": "in_force"}, ...]}

    Transitions are applied in order; the last one whose date <= today wins.
    """
    status: str = rules["default"]
    for transition in rules.get("transitions", []):
        transition_date = date.fromisoformat(transition["on"])
        if today >= transition_date:
            status = transition["to"]
    return status
