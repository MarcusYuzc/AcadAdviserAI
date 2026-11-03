"""Assemble site pages from their shared sidebar and footer."""
from __future__ import annotations

import html
from pathlib import Path

SITE = Path(__file__).resolve().parent / "site"


def contact_html(email: str) -> str:
    email = email.strip()
    if not email:
        return "our contact address (being set up)"
    return f'<a href="mailto:{html.escape(email, quote=True)}">{html.escape(email)}</a>'


def render(name: str, active: str, contact_email: str = "") -> str:
    sidebar = (SITE / "_sidebar.html").read_text(encoding="utf-8")
    sidebar = sidebar.replace(f'data-nav="{active}"', 'class="active" aria-current="page"')
    page = (SITE / name).read_text(encoding="utf-8")
    page = page.replace("{{SIDEBAR}}", sidebar)
    page = page.replace("{{FOOTER}}", (SITE / "_footer.html").read_text(encoding="utf-8"))
    return page.replace("{{CONTACT}}", contact_html(contact_email))
