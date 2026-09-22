# utils/email_templates.py
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "email"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)

_DEFAULT_LOCALE = "es"


def render_email(template_name: str, locale: str = _DEFAULT_LOCALE, **context) -> str:
    """Render an HTML email body from a named template in templates/email/.

    Locale-aware resolution: for ``template_name`` "confirmation.html" and
    ``locale`` "en", tries ``confirmation.en.html``, then falls back to
    ``confirmation.es.html`` (the default locale), then to the plain
    ``confirmation.html``. Templates without locale variants (payment emails,
    join-request decision) resolve unchanged via the last step.
    """
    stem = template_name[:-5] if template_name.endswith(".html") else template_name
    candidates = [f"{stem}.{locale}.html", f"{stem}.{_DEFAULT_LOCALE}.html", f"{stem}.html"]
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            template = _env.get_template(candidate)
        except TemplateNotFound:
            continue
        return template.render(**context)
    raise TemplateNotFound(template_name)
