# utils/playbook_expr.py
"""Declared integer arithmetic for playbooks (doc 40 §3.3.3, ADR-006 amendment).

A playbook may declare a `computed` block:

    "computed": [{"key": "onu_id",
                  "expr": "(path.mufa_principal.out_port - 1) * 16 + path.mufa_secundaria.out_port",
                  "min": 1, "max": 128}]

and templates read the result by plain lookup, `{{computed.onu_id}}`. The
renderer stays a dictionary lookup; arithmetic happens here, once, before any
device is touched.

WHAT THIS IS NOT. There is no `eval`, `ast`, `compile` or `format` anywhere in
this module. A hand-written tokenizer feeds a recursive-descent parser that
returns plain tuples, and a 20-line walker evaluates them over integers:

    expr  := term (("+" | "-") term)*
    term  := unary (("*" | "/" | "%") unary)*
    unary := "-" unary | atom
    atom  := INT | NAME | "(" expr ")"
    INT   := [0-9]{1,9}
    NAME  := NS ("." [a-z][a-z0-9_]*)+     NS in NAMESPACES

`input.*` is deliberately not a namespace here: author variables are resolved
by the backend renderer, so the resolver could not see them and a computed
value would differ between resolution and render.

Semantics are integer-only and chosen so the TypeScript mirror
(frontend-erp/lib/playbookExpr.ts) is exact: `/` truncates toward zero, `%` is
`a - b*trunc(a/b)`, and every operand, intermediate value and result must
satisfy |x| <= 2**31 - 1. Both implementations are pinned by the hash-locked
fixture tests/fixtures/playbook_expr.json.

Error codes (the contract; branch on these, not on the prose):
  COMPUTE_SYNTAX    a character or token sequence the grammar does not accept
  COMPUTE_LIMIT     an expression over 256 characters, 64 tokens or depth 8
  COMPUTE_NAME      a name outside NAMESPACES (including input.*), or a
                    computed.* name that is not an earlier key
  COMPUTE_SECRET    a secret-named operand (is_secret_name)
  COMPUTE_TYPE      an operand that is not an int or a decimal-integer string
  COMPUTE_OVERFLOW  |x| > 2**31 - 1 at any point
  COMPUTE_DIV_ZERO  division or modulo by zero
  COMPUTE_RANGE     a result outside the entry's min/max
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# --- secret names -----------------------------------------------------------
# Moved here from backend-erp provisioning/renderer.py (doc 40 §3.3.3) so the
# schemas can refuse secret-named path roles and computed keys with the same
# rule the renderer uses to mask values. renderer.py re-exports both names.
_SECRET_HINTS = ("password", "secret", "token", "key", "credential")


def is_secret_name(name: str) -> bool:
    return any(hint in name.lower() for hint in _SECRET_HINTS)


# --- limits -----------------------------------------------------------------
NAMESPACES = frozenset(
    {"device", "cpe", "path", "service_plan", "client", "service", "computed"}
)
COMPUTED_NAMESPACE = "computed"
MAX_ENTRIES = 16
MAX_EXPR_LENGTH = 256
MAX_TOKENS = 64
MAX_DEPTH = 8
INT_LIMIT = 2**31 - 1
KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,31}")

_SEGMENT = r"[a-z][a-z0-9_]*"
_NAME = re.compile(r"(" + _SEGMENT + r")((?:\." + _SEGMENT + r")+)")
_OPERAND_STR = re.compile(r"-?[0-9]{1,9}")
_OPERATORS = "+-*/%()"


class ExprError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


# --- tokenizer --------------------------------------------------------------

def _tokenize(expr: str) -> list[tuple[str, Any]]:
    if not isinstance(expr, str):
        raise ExprError("COMPUTE_SYNTAX", "expression must be a string")
    if len(expr) > MAX_EXPR_LENGTH:
        raise ExprError("COMPUTE_LIMIT", f"expression longer than {MAX_EXPR_LENGTH} characters")
    tokens: list[tuple[str, Any]] = []
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch in " \t":
            i += 1
            continue
        if ch in _OPERATORS:
            tokens.append(("op", ch))
            i += 1
        elif "0" <= ch <= "9":
            j = i
            while j < n and "0" <= expr[j] <= "9":
                j += 1
            if j - i > 9:
                raise ExprError("COMPUTE_SYNTAX", "integer literal longer than 9 digits")
            tokens.append(("int", int(expr[i:j])))
            i = j
        elif "a" <= ch <= "z":
            j = i
            while j < n and (expr[j] in "_." or "a" <= expr[j] <= "z" or "0" <= expr[j] <= "9"):
                j += 1
            word = expr[i:j]
            match = _NAME.fullmatch(word)
            if match is None or match.group(1) not in NAMESPACES:
                raise ExprError(
                    "COMPUTE_NAME",
                    f"'{word}' is not a variable of {sorted(NAMESPACES)}",
                )
            if match.group(1) == COMPUTED_NAMESPACE and match.group(2).count(".") != 1:
                raise ExprError("COMPUTE_NAME", f"'{word}' is not a computed key")
            tokens.append(("name", word))
            i = j
        else:
            raise ExprError("COMPUTE_SYNTAX", f"character {ch!r} is not allowed")
        if len(tokens) > MAX_TOKENS:
            raise ExprError("COMPUTE_LIMIT", f"expression longer than {MAX_TOKENS} tokens")
    return tokens


# --- parser -----------------------------------------------------------------

class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0
        self.depth = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def take(self):
        tok = self.peek()
        self.pos += 1
        return tok

    def expr(self):
        node = self.term()
        while self.peek() in (("op", "+"), ("op", "-")):
            node = (self.take()[1], node, self.term())
        return node

    def term(self):
        node = self.unary()
        while self.peek() in (("op", "*"), ("op", "/"), ("op", "%")):
            node = (self.take()[1], node, self.unary())
        return node

    def unary(self):
        if self.peek() == ("op", "-"):
            self.take()
            return ("neg", self.unary())
        return self.atom()

    def atom(self):
        kind, value = self.take()
        if kind == "int":
            return ("int", value)
        if kind == "name":
            return ("name", value)
        if (kind, value) == ("op", "("):
            self.depth += 1
            if self.depth > MAX_DEPTH:
                raise ExprError("COMPUTE_LIMIT", f"parentheses nested deeper than {MAX_DEPTH}")
            node = self.expr()
            if self.take() != ("op", ")"):
                raise ExprError("COMPUTE_SYNTAX", "missing ')'")
            self.depth -= 1
            return node
        found = "end of expression" if kind is None else repr(value)
        raise ExprError("COMPUTE_SYNTAX", f"unexpected {found}")


def parse(expr: str) -> tuple:
    """Expression text -> tuple tree. Raises ExprError."""
    parser = _Parser(_tokenize(expr))
    tree = parser.expr()
    if parser.pos != len(parser.tokens):
        raise ExprError("COMPUTE_SYNTAX", f"unexpected {parser.peek()[1]!r}")
    return tree


def names(tree: tuple) -> list[str]:
    """Variable names a tree reads, first occurrence order, de-duplicated."""
    out: list[str] = []
    stack = [tree]
    while stack:
        node = stack.pop()
        if node[0] == "name":
            if node[1] not in out:
                out.append(node[1])
        elif node[0] != "int":
            stack.extend(reversed(node[1:]))
    return out


# --- evaluation -------------------------------------------------------------

def _bounded(x: int) -> int:
    if -INT_LIMIT <= x <= INT_LIMIT:
        return x
    raise ExprError("COMPUTE_OVERFLOW", f"{x} exceeds |2^31 - 1|")


def coerce_operand(name: str, value: Any) -> int:
    """An int (never a bool) or a decimal-integer string; anything else is
    COMPUTE_TYPE. fullmatch, not match: `$` would accept a trailing newline."""
    if is_secret_name(name):
        raise ExprError("COMPUTE_SECRET", f"'{name}' is secret-named")
    if isinstance(value, bool):
        raise ExprError("COMPUTE_TYPE", f"'{name}' is a boolean, not an integer")
    if isinstance(value, int):
        return _bounded(value)
    if isinstance(value, str) and _OPERAND_STR.fullmatch(value):
        return int(value)
    raise ExprError("COMPUTE_TYPE", f"'{name}' is not an integer")


def _trunc_div(a: int, b: int) -> int:
    if b == 0:
        raise ExprError("COMPUTE_DIV_ZERO", "division by zero")
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def _evaluate(node: tuple, env: dict[str, int]) -> int:
    op = node[0]
    if op == "int":
        return node[1]
    if op == "name":
        return env[node[1]]
    if op == "neg":
        return _bounded(-_evaluate(node[1], env))
    a = _evaluate(node[1], env)
    b = _evaluate(node[2], env)
    if op == "+":
        return _bounded(a + b)
    if op == "-":
        return _bounded(a - b)
    if op == "*":
        return _bounded(a * b)
    if op == "/":
        return _bounded(_trunc_div(a, b))
    return _bounded(a - b * _trunc_div(a, b))  # "%"


def _field(entry: Any, name: str) -> Any:
    """Entries are ComputedVar models or the raw dicts stored in a definition."""
    return (entry if isinstance(entry, dict) else entry.model_dump()).get(name)


def evaluate_all(
    computed: Iterable[Any], variables: dict[str, Any]
) -> tuple[dict[str, int], list[str], list[dict[str, Any]]]:
    """Evaluate a declared `computed` block over one flat variable dict.

    Returns (values, missing, errors):
      values   {"computed.<key>": int} for every entry that evaluated
      missing  operand names absent (or None) in `variables`, de-duplicated
      errors   [{"code", "key", "detail"}] for every entry that failed

    An entry whose operand is missing, or that reads an earlier entry which
    failed, is skipped without a second error: the root cause is already
    reported once. Callers treat any missing name or error as fatal.
    """
    values: dict[str, int] = {}
    missing: list[str] = []
    errors: list[dict[str, Any]] = []
    declared: set = set()
    for entry in computed or []:
        key = _field(entry, "key")
        declared.add(f"{COMPUTED_NAMESPACE}.{key}")
        try:
            tree = parse(_field(entry, "expr"))
            env: dict[str, int] = {}
            skip = False
            for name in names(tree):
                if is_secret_name(name):
                    raise ExprError("COMPUTE_SECRET", f"'{name}' is secret-named")
                if name.startswith(COMPUTED_NAMESPACE + "."):
                    if name in values:
                        env[name] = values[name]
                    elif name in declared and name != f"{COMPUTED_NAMESPACE}.{key}":
                        skip = True  # an earlier entry failed; already reported
                    else:
                        raise ExprError("COMPUTE_NAME", f"'{name}' is not an earlier computed key")
                elif variables.get(name) is None:
                    if name not in missing:
                        missing.append(name)
                    skip = True
                else:
                    env[name] = coerce_operand(name, variables[name])
            if skip:
                continue
            result = _evaluate(tree, env)
            low, high = _field(entry, "min"), _field(entry, "max")
            if (low is not None and result < low) or (high is not None and result > high):
                raise ExprError("COMPUTE_RANGE", f"{result} is outside [{low}, {high}]")
            values[f"{COMPUTED_NAMESPACE}.{key}"] = result
        except ExprError as exc:
            errors.append({"code": exc.code, "key": key, "detail": exc.detail})
    return values, missing, errors
