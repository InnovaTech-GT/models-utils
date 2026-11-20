"""SNMP traffic match template (doc 74).

A tenant links a router counter (a MikroTik queue name, an interface ifName)
to a service with a template written in the playbook language, e.g.
``<pppoe-{{client.code | alnum}}>``. It is rendered per service over
provisioning_resolution.service_variables and compared with the counter name,
exactly. Only variables fixed for a service are allowed: a run's input.*,
capture.*, secret.*, computed.* and the network path's frames are not.
"""
from typing import Any, Dict, Optional

MATCH_NAMESPACES = ("client.", "service.", "service_plan.")
MAX_TEMPLATE_LEN = 200


def check_match_template(template: str) -> None:
    """Raise ValueError("TEMPLATE_*: ...") when the template can't be used."""
    # Lazy: playbook_template imports provisioning_resolution, which imports
    # the models; schemas/company.py calls this from a validator.
    from database_utils.utils.playbook_template import TOKEN_SHAPE, TemplateError, parse_body

    if len(template) > MAX_TEMPLATE_LEN:
        raise ValueError(f"TEMPLATE_TOO_LONG: at most {MAX_TEMPLATE_LEN} characters")
    bodies = [m.group("body") for m in TOKEN_SHAPE.finditer(template)]
    if not bodies:
        raise ValueError("TEMPLATE_NO_TOKEN: use at least one variable, e.g. {{client.code}}")
    for body in bodies:
        try:
            expr = parse_body(body)
        except TemplateError as exc:
            raise ValueError(f"TEMPLATE_INVALID: {{{{{body.strip()}}}}}: {exc}") from None
        if not expr.name.startswith(MATCH_NAMESPACES):
            raise ValueError(
                f"TEMPLATE_NAMESPACE: {{{{{expr.name}}}}} is not fixed for a service; "
                "use client.*, service.* or service_plan.*")


def render_match_key(template: str, variables: Dict[str, Any]) -> Optional[str]:
    """The counter name this service answers to, or None when a variable is
    missing or empty (an empty client code must not match a bare prefix)."""
    from database_utils.utils.playbook_template import render_string

    rendered, problems = render_string(template, variables)
    if problems:
        return None
    rendered = rendered.strip()
    return rendered or None

