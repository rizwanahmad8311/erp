"""Shared enumerations. The document lifecycle is locked; see CLAUDE.md."""

from django.db import models


class DocumentStatus(models.TextChoices):
    """DRAFT -> POSTED -> CANCELLED. No other transitions exist.

    DRAFT     editable, has written nothing to any ledger
    POSTED    immutable, ledger and stock rows exist for it
    CANCELLED immutable, reversing ledger and stock rows exist for it
    """

    DRAFT = "DRAFT", "Draft"
    POSTED = "POSTED", "Posted"
    CANCELLED = "CANCELLED", "Cancelled"


# The only legal moves. Anything not listed is a bug, not a business case.
ALLOWED_STATUS_TRANSITIONS: dict[str, set[str]] = {
    DocumentStatus.DRAFT: {DocumentStatus.POSTED},
    DocumentStatus.POSTED: {DocumentStatus.CANCELLED},
    DocumentStatus.CANCELLED: set(),
}
