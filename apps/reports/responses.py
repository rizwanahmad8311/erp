"""Turning PDF bytes into an HTTP response, once.

Two things every ``?format=pdf`` route needs and would otherwise each get
slightly wrong:

* **the filename**, which is what the file is called in somebody's Downloads
  folder and what an emailed attachment shows up as — so it carries the document
  code, not "document.pdf";
* **inline versus attachment**, which decides whether the browser opens its own
  viewer (and its Print button — the fastest route to paper on a machine that
  already has the PDF open) or drops the file on disk for emailing.

Inline is the default. ``?download=1`` asks for the file.
"""

from __future__ import annotations

import re

from django.http import HttpResponse

#: Anything that is not one of these is replaced. Windows refuses several of
#: them in a filename outright, and a document code with a slash in it would
#: quietly become a path.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def pdf_filename(*parts: str) -> str:
    """``pdf_filename("SI-2026-000123", "Al Madina")`` -> ``SI-2026-000123-Al-Madina.pdf``."""
    cleaned = [_UNSAFE.sub("-", str(part).strip()).strip("-") for part in parts if part]
    return "-".join(filter(None, cleaned))[:120] + ".pdf"


def pdf_response(content: bytes, filename: str, *, download: bool = False) -> HttpResponse:
    """A PDF, either opened in the browser or saved to disk.

    ``Content-Length`` is set explicitly: without it the browser cannot show a
    progress bar or, more usefully here, tell that a truncated file is
    truncated.
    """
    disposition = "attachment" if download else "inline"
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    response["Content-Length"] = str(len(content))
    return response


def wants_pdf(request) -> bool:
    """Whether this request asked for the PDF rather than the screen."""
    return (request.GET.get("format") or "").strip().lower() == "pdf"


def wants_download(request) -> bool:
    value = (request.GET.get("download") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


__all__ = ["pdf_filename", "pdf_response", "wants_download", "wants_pdf"]
