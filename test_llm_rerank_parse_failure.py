#!/usr/bin/env python3
"""
Test TP12.13 / Task 8.5: LLM Rerank parse failure must NOT be silently ignored.
- Invalid JSON -> raise LLMRerankParseError.
- Debug file created and contains raw content.
"""
import os
import tempfile

import pytest

from map_knowledge_to_questions import (
    LLMRerankParseError,
    _parse_llm_rerank_json,
)


def test_parse_valid_json():
    """Valid JSON should return parsed dict."""
    raw = '{"is_related": true, "related_indices": [1], "reason": "ok"}'
    with tempfile.TemporaryDirectory() as tmp:
        out = _parse_llm_rerank_json(raw, raw, debug_dir=tmp)
    assert out["is_related"] is True
    assert out["related_indices"] == [1]
    assert out["reason"] == "ok"


def test_parse_invalid_json_raises():
    """Invalid JSON must raise LLMRerankParseError (no silent ignore)."""
    invalid = '{"is_related": true, "reason": "未闭合'
    raw = f"# Raw\n{invalid}"
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(LLMRerankParseError) as exc_info:
            _parse_llm_rerank_json(invalid, raw, debug_dir=tmp)
        e = exc_info.value
        assert "parse failed" in str(e).lower() or "json" in str(e).lower()
        assert e.debug_path.endswith(".txt")
        assert os.path.isabs(e.debug_path) or os.path.exists(e.debug_path)


def test_parse_invalid_json_debug_file_exists_and_has_raw():
    """On parse failure, debug file must exist and contain raw content (TP12.13)."""
    invalid = '{"is_related": true, "reason": "unterminated'
    raw = "raw llm output here\n" + invalid
    err = None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            _parse_llm_rerank_json(invalid, raw, debug_dir=tmp)
        except LLMRerankParseError as e:
            err = e
        if err is None:
            pytest.fail("Expected LLMRerankParseError")
        assert os.path.exists(err.debug_path)
        with open(err.debug_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "raw llm output" in content
        assert invalid in content
