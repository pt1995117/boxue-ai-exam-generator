from __future__ import annotations

import re
from pathlib import Path


def escape_prompt_braces(text: str, preserved_vars: list[str]) -> str:
    """Escape JSON braces for ChatPromptTemplate while preserving template vars."""
    out = text.replace("{", "{{").replace("}", "}}")
    for var in preserved_vars:
        out = out.replace("{{" + var + "}}", "{" + var + "}")
    return out


def load_prompt_pair(
    path: str | Path,
    default_system: str,
    default_human: str,
    preserved_vars: list[str],
) -> tuple[str, str]:
    """Load prompt pair from markdown:

    ## SYSTEM
    ...
    ## HUMAN
    ...
    """
    try:
        p = Path(path)
        if not p.exists():
            return default_system, default_human
        text = p.read_text(encoding="utf-8")
        m = re.search(r"##\s*SYSTEM\s*(.*?)##\s*HUMAN\s*(.*)$", text, flags=re.S)
        if not m:
            return default_system, default_human
        system_prompt = m.group(1).strip()
        human_prompt = m.group(2).strip()
        if not system_prompt or not human_prompt:
            return default_system, default_human
        return (
            escape_prompt_braces(system_prompt, preserved_vars),
            escape_prompt_braces(human_prompt, preserved_vars),
        )
    except Exception:
        return default_system, default_human

