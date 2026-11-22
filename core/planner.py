"""Pick the background agent that matches a task.

A research task needs the method that checks quotes against pages. Everything
else goes to the operator, which plans the requested parts, does them in order
and checks the result. `general` remains the fallback when the operator profile
is not installed.
"""
from __future__ import annotations

import re

_RESEARCH = re.compile(
    r"\b("
    r"research|investigate|compare|sources|in[- ]depth|cited|"
    r"tadqiq\w*|solishtir\w*|manbalar\w*|chuqur|"
    r"исслед\w*|сравн\w*|источник\w*"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)


def choose_agent(task: str) -> str:
    """`researcher` when the task is to gather and check sources, else `operator`."""
    if _RESEARCH.search(task or ""):
        return "researcher"
    return "operator"
