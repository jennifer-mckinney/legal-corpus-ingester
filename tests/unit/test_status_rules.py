from __future__ import annotations
from datetime import date


def test_resolve_status_before_transition() -> None:
    from legal_corpus_ingester.pipeline.status_rules import resolve_status
    rules = {
        "default": "not_yet_in_force",
        "transitions": [{"on": "2027-01-01", "to": "in_force"}],
    }
    result = resolve_status(rules, today=date(2026, 7, 4))
    assert result == "not_yet_in_force"


def test_resolve_status_on_transition_date() -> None:
    from legal_corpus_ingester.pipeline.status_rules import resolve_status
    rules = {
        "default": "not_yet_in_force",
        "transitions": [{"on": "2027-01-01", "to": "in_force"}],
    }
    result = resolve_status(rules, today=date(2027, 1, 1))
    assert result == "in_force"


def test_resolve_status_after_transition_date() -> None:
    from legal_corpus_ingester.pipeline.status_rules import resolve_status
    rules = {
        "default": "not_yet_in_force",
        "transitions": [{"on": "2027-01-01", "to": "in_force"}],
    }
    result = resolve_status(rules, today=date(2027, 6, 15))
    assert result == "in_force"


def test_multiple_transitions_apply_in_order() -> None:
    from legal_corpus_ingester.pipeline.status_rules import resolve_status
    rules = {
        "default": "proposed",
        "transitions": [
            {"on": "2026-01-01", "to": "adopted"},
            {"on": "2027-01-01", "to": "in_force"},
            {"on": "2030-01-01", "to": "superseded"},
        ],
    }
    assert resolve_status(rules, today=date(2025, 12, 31)) == "proposed"
    assert resolve_status(rules, today=date(2026, 6, 1)) == "adopted"
    assert resolve_status(rules, today=date(2027, 6, 1)) == "in_force"
    assert resolve_status(rules, today=date(2030, 6, 1)) == "superseded"


def test_no_transitions_returns_default() -> None:
    from legal_corpus_ingester.pipeline.status_rules import resolve_status
    rules = {"default": "in_force", "transitions": []}
    assert resolve_status(rules, today=date(2026, 7, 4)) == "in_force"
