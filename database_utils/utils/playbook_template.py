"""
Moved verbatim from backend-erp `provisioning/renderer.py` (provisioning
worker split, R1): the API (preview, lint, save-time checks) and the worker
(execution) import this one renderer, so preview and execution cannot drift.
backend-erp keeps `provisioning/renderer.py` as a re-export shim.

Playbook template rendering: sandboxed {{variable}} substitution with a closed
set of string filters. No expressions, no attribute access, no code execution
(ADR-006 security).

Namespaced names (doc 33): a token is a namespace plus a dotted attribute,
optionally indexed — `edge_devices[0].serial`, `service_plan.vlan`,
`client.name`, `service.id`, `input.probe_command`. Despite the dots and
brackets this is still NOT attribute access: the whole name is one literal KEY
in a flat dict, so resolution stays a dictionary lookup and cannot walk objects
or evaluate anything.

Filters (doc 34): a token body may carry a pipe chain —
`{{ client.name | trim | truncate:31 }}`, `{{ edge_devices[0].mac | mac:dash }}`.
This is deliberately NOT a template language:

  * Filter names are matched by exact equality against the FILTERS dict below.
    There is no getattr, no eval, no exec, no str.format anywhere in this
    module, and there must never be. A getattr-based dispatch would turn a
    tenant-writable playbook row into code execution inside the provisioning
    worker, which holds SSH credentials for every customer's core network.
  * Filter ARGUMENTS are literals only. An argument can never name another
    variable, so there is no nesting, no recursion and no evaluation order to
    reason about; the whole argument set is static and auditable at save time.
  * `{` and `}` cannot appear anywhere in a body, so no filter output can
    manufacture token-shaped text and no second rendering pass exists.

ONE recognizer, two jobs. `TOKEN_SHAPE` is the single source of truth for
"this text is {{…}}-shaped"; executor.py imports it for its unrendered-token
guard rather than defining its own. Before this, the guard and the renderer
each carried a pattern and could disagree — text that looked like a token to
one and not the other is precisely how an unsubstituted placeholder reaches a
device as literal config. A strict `_VAR_PATTERN` no longer exists.

Silent-literal behaviour is preserved for bodies with NO pipe: they are left
verbatim exactly as before, which is what keeps every stored playbook working.
A body WITH a pipe that fails to parse is a hard error — once an author has
reached for a filter, quietly shipping their broken template is the wrong
answer.
"""
import re
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

# The `input.*` helper lives in models-utils because the workflow engine
# produces author variables too and cannot import backend-erp.
from database_utils.utils.provisioning_resolution import INPUT_NAMESPACE, input_key  # noqa: F401
# One secret-name rule, shared with models-utils' path_role / computed-key
# checks (doc 40 §3.3.3). Re-exported: executor.py and drivers/cli.py import
# both names from here.
from database_utils.utils.playbook_expr import _SECRET_HINTS, is_secret_name  # noqa: F401
# Declared integer arithmetic (doc 40 §3.3.3). It lives in models-utils so the
# resolver evaluates the same block, with the same inputs, before a run starts.
from database_utils.utils.playbook_expr import evaluate_all

_NAME = r"[a-z][a-z0-9_]*"
_VAR_NAME = _NAME + r"(?:\[\d{1,3}\])?(?:\." + _NAME + r")*"

# LOOSE recognizer — the ONE source of truth for "this is token-shaped".
# Linear time: negated class, no nesting, no alternation, bounded length, so
# there is no ReDoS surface. Excluding braces and newlines from the body stops
# it swallowing a multi-line JSON blob between two unrelated brace pairs.
TOKEN_SHAPE = re.compile(r"\{\{(?P<body>[^{}\n]{0,512})\}\}")

_VAR_HEAD = re.compile(r"[ \t]*(" + _VAR_NAME + r")[ \t]*")
_FILTER_NAME = re.compile(r"[ \t]*(" + _NAME + r")[ \t]*")
_BARE_ARG = re.compile(r"[A-Za-z0-9_.+\-]+")


MAX_FILTERS = 4
MAX_ARG_LEN = 128
MAX_OUTPUT = 64 * 1024

_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "n": "\n", "t": "\t", "r": "\r"}


class TemplateError(Exception):
    """A template construct that cannot be parsed or is not permitted.

    Static — decidable without any variables — so it surfaces at save time and
    in the editor's lint strip, not only when a job runs against a device.
    """


class Expr(NamedTuple):
    name: str
    filters: Tuple[Tuple[str, Tuple[str, ...]], ...]


# --------------------------------------------------------------------------
# Filters — a closed table. Adding an entry is an ADR-006 amendment, not a
# routine change: it needs a security review, a frontend mirror entry, a
# palette label in both locales, and a shared-fixture case. That friction is
# the mechanism that keeps this from becoming a programming language.
# --------------------------------------------------------------------------

_MAC_STYLES = ("colon", "dash", "dot", "bare")


def _f_replace(s: str, old: str, new: str) -> str:
    if not old:
        raise TemplateError("'replace' needs a non-empty first argument")
    return s.replace(old, new)


def _f_truncate(s: str, n: str) -> str:
    return s[: _int_arg("truncate", n, 1, 512)]


def _f_last(s: str, n: str) -> str:
    """The last N characters (`{{cpe.serial | last:4}}` -> "6BA1")."""
    return s[-_int_arg("last", n, 1, 512):]


def _f_pad_left(s: str, width: str, fill: str = " ") -> str:
    if len(fill) != 1:
        raise TemplateError("'pad_left' fill must be exactly one character")
    # Never truncates: a value longer than the width is returned unchanged,
    # because silently cutting an identifier is worse than an over-long one.
    return s.rjust(_int_arg("pad_left", width, 1, 64), fill)


def _f_mac(s: str, style: str) -> str:
    if style not in _MAC_STYLES:
        raise TemplateError(
            f"'mac' style must be one of {', '.join(_MAC_STYLES)} (got '{style}')"
        )
    hex_digits = re.sub(r"[^0-9A-Fa-f]", "", s)
    if len(hex_digits) != 12:
        raise TemplateError(f"'{s}' is not a MAC address")
    if style == "dot":
        low = hex_digits.lower()
        return ".".join(low[i:i + 4] for i in range(0, 12, 4))
    up = hex_digits.upper()
    if style == "bare":
        return up
    sep = ":" if style == "colon" else "-"
    return sep.join(up[i:i + 2] for i in range(0, 12, 2))


def _f_alnum(s: str) -> str:
    """Keep [A-Za-z0-9_-]. The documented sanitizer for CRM-sourced text that
    reaches a device CLI, where a stray quote or newline is an injection."""
    return re.sub(r"[^A-Za-z0-9_-]", "", s)


def _int_arg(filter_name: str, raw: str, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise TemplateError(f"'{filter_name}' needs a whole number, got '{raw}'")
    if not (low <= value <= high):
        raise TemplateError(
            f"'{filter_name}' argument must be between {low} and {high} (got {value})"
        )
    return value


class FilterSpec(NamedTuple):
    fn: Any
    min_args: int
    max_args: int
    hint: str


FILTERS: Dict[str, FilterSpec] = {
    "upper": FilterSpec(lambda s: s.upper(), 0, 0, "upper"),
    "lower": FilterSpec(lambda s: s.lower(), 0, 0, "lower"),
    "trim": FilterSpec(lambda s: s.strip(), 0, 0, "trim"),
    "alnum": FilterSpec(_f_alnum, 0, 0, "alnum"),
    "replace": FilterSpec(_f_replace, 2, 2, 'replace:"old","new"'),
    "truncate": FilterSpec(_f_truncate, 1, 1, "truncate:32"),
    "last": FilterSpec(_f_last, 1, 1, "last:4"),
    "pad_left": FilterSpec(_f_pad_left, 1, 2, 'pad_left:3,"0"'),
    "mac": FilterSpec(_f_mac, 1, 1, "mac:colon|dash|dot|bare"),
    # `default` never reaches _apply — it is handled in the render loop because
    # it is the one filter with something to say about a MISSING value.
    "default": FilterSpec(None, 1, 1, 'default:"value"'),
}


def _suggest(name: str) -> str:
    """Cheap did-you-mean: an operator's typo is nearly always a near-miss."""
    for known in FILTERS:
        if known.startswith(name[:2]) or abs(len(known) - len(name)) <= 1 and sum(
            a != b for a, b in zip(known, name)
        ) <= 1:
            return f" Did you mean '{known}'?"
    return ""


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _skip_ws(body: str, i: int) -> int:
    while i < len(body) and body[i] in " \t":
        i += 1
    return i


def _parse_args(body: str, i: int) -> Tuple[Tuple[str, ...], int]:
    """Hand-written scanner. Argument parsing is exactly where a single clever
    regex accumulates quoting bugs, so it is deliberately explicit."""
    args: List[str] = []
    while True:
        i = _skip_ws(body, i)
        if i >= len(body) or body[i] in "|,":
            raise TemplateError("empty argument")
        quote = body[i]
        if quote in "'\"":
            buf: List[str] = []
            i += 1
            while True:
                if i >= len(body):
                    raise TemplateError("unterminated quoted argument")
                char = body[i]
                if char == "\\":
                    if i + 1 >= len(body):
                        raise TemplateError("dangling backslash")
                    escape = body[i + 1]
                    if escape not in _ESCAPES:
                        raise TemplateError(f"invalid escape '\\{escape}'")
                    buf.append(_ESCAPES[escape])
                    i += 2
                    continue
                if char == quote:
                    i += 1
                    break
                buf.append(char)
                i += 1
            arg = "".join(buf)
        else:
            match = _BARE_ARG.match(body, i)
            if not match:
                raise TemplateError(f"unexpected {body[i]!r} in argument")
            arg, i = match.group(0), match.end()
        if len(arg) > MAX_ARG_LEN:
            raise TemplateError(f"argument too long (max {MAX_ARG_LEN})")
        args.append(arg)
        i = _skip_ws(body, i)
        if i < len(body) and body[i] == ",":
            i += 1
            continue
        # Anything other than a comma, a pipe or end-of-body is junk — report
        # it rather than silently truncating the author's intent.
        if i < len(body) and body[i] != "|":
            raise TemplateError(f"unexpected {body[i]!r} after argument")
        return tuple(args), i


def parse_body(body: str) -> Expr:
    """Parse a `{{ … }}` body into a name plus its filter chain."""
    head = _VAR_HEAD.match(body)
    if not head:
        raise TemplateError("not a variable reference")
    name, i = head.group(1), head.end()
    filters: List[Tuple[str, Tuple[str, ...]]] = []
    while i < len(body):
        if body[i] != "|":
            raise TemplateError(f"unexpected {body[i]!r} after variable")
        fname_match = _FILTER_NAME.match(body, i + 1)
        if not fname_match:
            raise TemplateError("missing filter name after '|'")
        fname, i = fname_match.group(1), fname_match.end()
        if fname not in FILTERS:
            raise TemplateError(f"unknown filter '{fname}'.{_suggest(fname)}")
        spec = FILTERS[fname]
        args: Tuple[str, ...] = ()
        if i < len(body) and body[i] == ":":
            args, i = _parse_args(body, i + 1)
        if not (spec.min_args <= len(args) <= spec.max_args):
            raise TemplateError(
                f"'{fname}' takes {spec.min_args}"
                + ("" if spec.min_args == spec.max_args else f"-{spec.max_args}")
                + f" argument(s), got {len(args)} — e.g. {spec.hint}"
            )
        # A transformed secret cannot be scrubbed from a job log: redaction
        # matches on the raw plaintext, so `{{ input.wifi_password | upper }}`
        # would write an unmaskable derivative into provisioning_job.log.
        # Refusing is the mechanism; there is no escape hatch.
        if is_secret_name(name):
            raise TemplateError(
                f"filters are not allowed on secret variable '{name}' "
                "(a transformed secret cannot be scrubbed from the job log). "
                "If the device needs it transformed, store it that way."
            )
        filters.append((fname, args))
        if len(filters) > MAX_FILTERS:
            raise TemplateError(f"too many filters (max {MAX_FILTERS})")
    return Expr(name, tuple(filters))


def _apply(value: str, fname: str, args: Tuple[str, ...]) -> str:
    """Dispatch by exact dict key. Never getattr, never eval."""
    out = FILTERS[fname].fn(value, *args)
    if len(out) > MAX_OUTPUT:
        raise TemplateError(f"'{fname}' produced more than {MAX_OUTPUT} characters")
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def find_variables(text: str) -> List[str]:
    """All variable NAMES referenced in a template string, first-seen order.

    Unparseable bodies are skipped — they are reported by
    find_template_errors(), not smuggled in here as fake variable names.
    """
    names: List[str] = []
    for match in TOKEN_SHAPE.finditer(text or ""):
        try:
            names.append(parse_body(match.group("body")).name)
        except TemplateError:
            continue
    return list(dict.fromkeys(names))


def find_template_errors(text: str) -> List[str]:
    """Static problems in a template, decidable without any variables.

    Used at save time so a typo never reaches a stored definition, and by the
    editor's lint strip. A body with NO pipe that fails to parse is NOT an
    error here: that is ordinary literal text (`{{ not a token }}`) and has
    always been left alone.
    """
    problems: List[str] = []
    for match in TOKEN_SHAPE.finditer(text or ""):
        body = match.group("body")
        if "|" not in body:
            continue
        try:
            parse_body(body)
        except TemplateError as exc:
            problems.append(f"{match.group(0)} — {exc}")
    return list(dict.fromkeys(problems))


def render_string(template: str, variables: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Substitute {{…}} constructs. Returns (rendered, problems).

    `problems` merges missing variable NAMES (unchanged spelling, so existing
    callers keep working) with template errors. Every caller already treats a
    non-empty list as fatal before dispatching to a driver.
    """
    problems: List[str] = []

    def _sub(match: "re.Match") -> str:
        body = match.group("body")
        try:
            expr = parse_body(body)
        except TemplateError as exc:
            # No pipe -> this was never a token; preserve the historical
            # silent-literal behaviour. With a pipe, the author reached for a
            # filter and deserves to be told.
            if "|" in body:
                problems.append(f"{match.group(0)} — {exc}")
            return match.group(0)

        present = expr.name in variables and variables[expr.name] is not None
        value = str(variables[expr.name]) if present else None

        for index, (fname, args) in enumerate(expr.filters):
            if fname == "default":
                if value is None or not value.strip():
                    value = args[0]
                continue
            if value is None:
                # Missing: skip the rest of the chain and report, unless a
                # later `default` rescues it.
                if any(f == "default" for f, _ in expr.filters[index:]):
                    continue
                break
            try:
                value = _apply(value, fname, args)
            except TemplateError as exc:
                problems.append(f"{match.group(0)} — {exc}")
                return match.group(0)

        if value is None:
            problems.append(expr.name)
            return match.group(0)
        return value

    return TOKEN_SHAPE.sub(_sub, template or ""), list(dict.fromkeys(problems))


def render_value(value: Any, variables: Dict[str, Any]) -> Tuple[Any, List[str]]:
    """Recursively render strings inside dicts/lists (http request payloads)."""
    if isinstance(value, str):
        return render_string(value, variables)
    if isinstance(value, dict):
        problems: List[str] = []
        out = {}
        for k, v in value.items():
            out[k], m = render_value(v, variables)
            problems.extend(m)
        return out, list(dict.fromkeys(problems))
    if isinstance(value, list):
        problems = []
        items = []
        for v in value:
            r, m = render_value(v, variables)
            items.append(r)
            problems.extend(m)
        return items, list(dict.fromkeys(problems))
    return value, []


_RESERVED_PREFIXES = ("computed.", "capture.", "secret.", "acs.")


def resolve_variables(
    definition: dict, provided: Optional[Dict[str, Any]]
) -> Tuple[Dict[str, Any], List[str]]:
    """Merge provided values over declared defaults.

    Declared variables are emitted under `input.*`; callers may supply them
    either bare (`{"vlan": 10}` — the shape every existing caller uses) or
    already-namespaced. Missing-required keys are reported with the bare
    declared key, which is what the author typed and what the UI shows.
    Then the definition's declared `computed` block (doc 40 §3.3.3) is
    evaluated over the merged values and its results land as
    `computed.<key>`, read by templates as a plain lookup. A computed failure
    or a missing operand joins the missing list, so the job fails before its
    first step, dry runs included — the resolver already refused it once at
    run creation; this is the backstop for jobs created any other way.
    Returns (resolved, missing_required_keys)."""
    provided = provided or {}
    resolved: Dict[str, Any] = {}
    missing: List[str] = []
    declared = definition.get("variables") or []
    for var in declared:
        key = var.get("key")
        namespaced = input_key(key)
        if key in provided:
            resolved[namespaced] = provided[key]
        elif namespaced in provided:
            resolved[namespaced] = provided[namespaced]
        elif var.get("default") is not None:
            resolved[namespaced] = var["default"]
        elif var.get("required"):
            missing.append(key)
    # Pass through extra provided values (the resolver's namespaced system
    # variables, and anything a caller injected directly) — except computed.*:
    # only evaluate_all below may produce those, so a caller can never place a
    # non-integer value behind a {{computed.x}} token (security review F1).
    # Engine v2 (doc 42 §4.4): capture.*, secret.* and acs.* likewise — the
    # executor adds them from dedicated parameters (charset-gated captures, the
    # run's decrypted secrets; acs.* loaded by the worker at claim, §9.7),
    # never from job.variables.
    for k, v in provided.items():
        if str(k).startswith(_RESERVED_PREFIXES):
            continue
        resolved.setdefault(k, v)
    values, absent, errors = evaluate_all(definition.get("computed") or [], resolved)
    resolved.update(values)
    missing.extend(absent)
    missing.extend(f"computed.{e['key']}: {e['code']} {e['detail']}" for e in errors)
    return resolved, missing


def mask_secrets(variables: Dict[str, Any]) -> Dict[str, Any]:
    """Mask secret-looking variables before persisting to job logs."""
    return {
        k: ("***" if is_secret_name(k) else v)
        for k, v in variables.items()
    }
