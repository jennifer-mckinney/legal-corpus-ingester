# tests/unit/test_logging.py
from __future__ import annotations
import json
import logging
import io
from legal_corpus_ingester.utils.logging import configure_logging, get_logger

def test_configure_logging_emits_json():
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream, format="json")
    log = get_logger("test.mod")
    log.info("hello", extra={"source": "eurlex", "chunk_count": 42})
    line = stream.getvalue().strip().splitlines()[-1]
    record = json.loads(line)
    assert record["level"] == "INFO"
    assert record["message"] == "hello"
    assert record["logger"] == "test.mod"
    assert record["source"] == "eurlex"
    assert record["chunk_count"] == 42
    assert "timestamp" in record

def test_configure_logging_default_text():
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream, format="text")
    log = get_logger("test.mod")
    log.info("hello")
    line = stream.getvalue().strip().splitlines()[-1]
    assert "INFO" in line and "hello" in line

def test_get_logger_returns_module_scoped():
    log_a = get_logger("mod.a")
    log_b = get_logger("mod.b")
    assert log_a.name == "mod.a"
    assert log_b.name == "mod.b"
    assert log_a is get_logger("mod.a")  # same instance on re-call
