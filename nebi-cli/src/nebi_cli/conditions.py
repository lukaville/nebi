"""`nebi wait` condition grammar: `field<op>value` clauses joined by `,` / `&&` / `and` (all must hold)."""

from __future__ import annotations

import re
from typing import Any

from nebi_cli.errors import AgentError
from nebi_cli.spec_model import EXIT_USAGE

Clause = tuple[str, str, Any]

_COND_RE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*(<=|>=|!=|==|=|<|>)\s*(.+?)\s*$")


def _coerce(v: str) -> Any:
    lv = v.lower()
    if lv in ("true", "yes", "on"):
        return True
    if lv in ("false", "no", "off"):
        return False
    try:
        return float(v)
    except ValueError:
        return v.strip("'\"")


def parse_condition(expr: str) -> list[Clause]:
    clauses = []
    for part in re.split(r"\s*(?:,|&&|\band\b)\s*", expr.strip()):
        if not part:
            continue
        m = _COND_RE.match(part)
        if not m:
            raise AgentError(
                "BAD_CONDITION",
                f"Cannot parse condition clause {part!r}.",
                hint="Use field<op>value, e.g. posture=BALANCING, battery>30, targets>0 (comma = AND).",
                exit_code=EXIT_USAGE,
            )
        clauses.append((m.group(1), m.group(2), _coerce(m.group(3))))
    if not clauses:
        raise AgentError("BAD_CONDITION", "Empty condition.", exit_code=EXIT_USAGE)
    return clauses


def eval_clause(actual: Any, op: str, expected: Any) -> bool:
    """Lists compare by length (or joined text for string values); bools and numbers by value; the rest as text."""
    if isinstance(actual, list):
        actual = len(actual) if not isinstance(expected, str) else ",".join(map(str, actual))
    a: Any
    e: Any
    if isinstance(expected, bool) or isinstance(actual, bool):
        a, e = bool(actual), bool(expected) if not isinstance(expected, str) else expected.lower() == "true"
    elif isinstance(expected, float) and isinstance(actual, (int, float)):
        a, e = float(actual), expected
    else:
        a, e = str(actual).lower(), str(expected).lower()
        if op in ("=", "=="):
            return a == e
        if op == "!=":
            return a != e
        return False
    return {
        "=": a == e,
        "==": a == e,
        "!=": a != e,
        "<": a < e,
        ">": a > e,
        "<=": a <= e,
        ">=": a >= e,
    }[op]


_eval_clause = eval_clause  # historical name (tests import it via nebi_cli.ops)
