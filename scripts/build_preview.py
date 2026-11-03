#!/usr/bin/env python3
"""Build the public static site into build/preview. Profile entry is left out."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadadviser.config import get_settings  # noqa: E402
from acadadviser.pages import SITE, render  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build/preview"
PAGES = {
    "index.html": ("home.html", "home"),
    "signin.html": ("signin.html", "signin"),
    "account.html": ("account.html", "account"),
    "privacy.html": ("privacy.html", "privacy"),
    "terms.html": ("terms.html", "terms"),
}


def static_links(text: str) -> str:
    """S3 serves files, so the gateway's clean paths become .html files."""
    for path in ("signin", "privacy", "terms"):
        text = text.replace(f'href="/{path}"', f'href="/{path}.html"')
    return text


def not_found(page: str) -> str:
    start, end = page.index("<main"), page.index("</main>")
    return (
        page[:start]
        + '<main class="stack legal"><h1>Page not found</h1><p><a href="/">Back to the home page</a></p>'
        + page[end:]
    )


def build(contact_email: str = "") -> Path:
    shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "static").mkdir(parents=True)
    for name in ("styles.css", "favicon.svg", "auth.js"):
        shutil.copy(SITE / name, OUT / "static" / name)
    outputs = {name: render(source, active, contact_email) for name, (source, active) in PAGES.items()}
    outputs["404.html"] = not_found(outputs["terms.html"])
    for name, page in outputs.items():
        (OUT / name).write_text(static_links(page), encoding="utf-8")
    return OUT


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else get_settings().contact_email))
