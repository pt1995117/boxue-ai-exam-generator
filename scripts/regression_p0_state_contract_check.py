#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def check_critic_required_fixes_backfill() -> None:
    source = (ROOT / "exam_graph.py").read_text(encoding="utf-8")
    assert 'payload["critic_required_fixes"] = list(dict.fromkeys(normalized_required_fixes))' in source
    assert '"critic_required_fixes": ["generation_mode"]' in source
    assert '"critic_required_fixes": ["duplicate_stem"]' in source


def check_fixer_exception_clears_unmet() -> None:
    source = (ROOT / "exam_graph.py").read_text(encoding="utf-8")
    assert '"fix_required_unmet": False' in source
    assert source.count('"fix_required_unmet": False') >= 3


def check_admin_trace_sync_contract() -> None:
    source = (ROOT / "admin_api.py").read_text(encoding="utf-8")
    assert source.count('("final_json" in state_update)') >= 2
    assert source.count("elif q_json is None:") >= 2


def main() -> None:
    check_critic_required_fixes_backfill()
    check_fixer_exception_clears_unmet()
    check_admin_trace_sync_contract()
    print("OK: P0 state-contract regression checks passed.")


if __name__ == "__main__":
    main()
