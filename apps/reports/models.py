"""The company's own details, for the top of every printed page.

One model, one row. Everything else in :mod:`apps.reports` is read-only
aggregation over the ledger (CLAUDE.md §6) and holds no state at all; this is
the exception, and it is not a ledger figure — it is the name, address and tax
number that go on a bill, which live nowhere else in the system.

**A singleton, enforced.** ``pk`` is pinned to 1 by :meth:`CompanyProfile.save`
and :meth:`CompanyProfile.get`, so "which of the two company profiles is the
real one" is a question that cannot be asked. The admin adds the row on first
open and never offers a second.
"""

from __future__ import annotations

from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models

from apps.core.models import TimeStampedModel

#: The one row's primary key. Named rather than spelled ``1`` in four places.
SINGLETON_PK = 1

#: What a logo may be. JPEG and PNG only: both are read by ReportLab through
#: Pillow, which arrives as a dependency of ReportLab itself, and both are what
#: a printer shop hands over. SVG is refused because ReportLab cannot draw one
#: without a converter that is not pure Python.
LOGO_EXTENSIONS = ["jpg", "jpeg", "png"]

#: Where an uploaded logo lands, under MEDIA_ROOT. A directory of its own so a
#: backup script can copy it without walking the whole media tree.
LOGO_UPLOAD_TO = "company/"


def logo_path_is_local(value) -> None:
    """Refuse anything that is not a plain filename under MEDIA_ROOT.

    A logo is **stored locally, never hotlinked** — CLAUDE.md §7. The production
    PC has no internet, so a URL here means every printed invoice either stalls
    or comes out with a hole where the logo should be. It is a ``FileField`` so
    a URL cannot normally get in at all; this catches the one way it can, which
    is a fixture or a data migration writing the ``name`` directly.
    """
    name = str(getattr(value, "name", value) or "")
    lowered = name.lower()
    if "://" in lowered or lowered.startswith("//"):
        raise ValidationError(
            "The logo must be an uploaded file, not a URL. The office PC has no internet, "
            "so a hotlinked image prints as a blank box."
        )


class CompanyProfile(TimeStampedModel):
    """Who this installation *is*, printed at the top of every document.

    Nothing here is validated against a tax authority and nothing is derived —
    it is typed once when the system is installed and corrected when the
    business moves. The one field with a rule attached is the logo, which must
    be a local file.
    """

    name = models.CharField(
        max_length=200,
        default="",
        help_text="The trading name, as it should appear on a bill.",
    )
    address = models.TextField(
        blank=True,
        default="",
        help_text="Street address, printed under the name. Line breaks are kept.",
    )
    phone = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="One or two numbers, comma separated. Printed in the header.",
    )
    email = models.EmailField(blank=True, default="")
    ntn = models.CharField(
        max_length=32,
        blank=True,
        default="",
        verbose_name="NTN",
        help_text="National Tax Number. Printed on every invoice; leave blank if unregistered.",
    )
    strn = models.CharField(
        max_length=32,
        blank=True,
        default="",
        verbose_name="STRN",
        help_text="Sales Tax Registration Number, when the business is sales-tax registered.",
    )

    logo = models.FileField(
        upload_to=LOGO_UPLOAD_TO,
        blank=True,
        null=True,
        validators=[FileExtensionValidator(LOGO_EXTENSIONS), logo_path_is_local],
        help_text=(
            "JPEG or PNG, stored on this machine. Roughly 600x200 prints well. "
            "Never a link to an image on the internet — this PC has none."
        ),
    )

    footer_text = models.TextField(
        blank=True,
        default="",
        help_text="Printed at the foot of every page, above the page number. Terms, a "
        "thank-you, a bank account — whatever the business puts on its bills.",
    )
    invoice_terms = models.TextField(
        blank=True,
        default="",
        help_text="Longer terms printed once at the end of an invoice, under the totals.",
    )

    class Meta:
        verbose_name = "company profile"
        verbose_name_plural = "company profile"

    def __str__(self) -> str:
        return self.name or "Company profile"

    # ------------------------------------------------------------------
    # The singleton
    # ------------------------------------------------------------------
    def save(self, *args, **kwargs):
        """Pin the row to ``pk=1``, whatever the caller thought it was doing.

        Not a ``clean()`` check: a second profile created from the shell or a
        data migration would bypass that, and two of these means two different
        company names depending on which query ran first.

        ``force_insert`` is dropped as well as the pk, which is what makes
        ``CompanyProfile.objects.create(...)`` an upsert onto row 1 rather than
        an ``IntegrityError``. A data migration that seeds the profile on an
        installation that already has one should overwrite it, not crash the
        migrate.

        The original ``created_at`` is carried across for the same reason: this
        row is created once and corrected thereafter, so an upsert from a fresh
        in-memory instance must not blank the stamp that says when the company
        was set up. ``auto_now_add`` only fills the field on an insert.
        """
        self.pk = SINGLETON_PK
        kwargs.pop("force_insert", None)
        if self.created_at is None:
            self.created_at = (
                type(self)
                .objects.filter(pk=SINGLETON_PK)
                .values_list("created_at", flat=True)
                .first()
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Refuse. Every printed page reads this row; clear the fields instead."""
        raise ValidationError(
            "The company profile is not deleted — every printed page reads it. "
            "Clear the fields you no longer want printed."
        )

    @classmethod
    def get(cls) -> CompanyProfile:
        """The one row, created empty on first use.

        Never raises. A fresh installation prints invoices with an empty header
        rather than a 500, and the header says what to do about it — see
        ``apps/reports/pdf/components.py``.
        """
        profile, _created = cls.objects.get_or_create(pk=SINGLETON_PK)
        return profile

    # ------------------------------------------------------------------
    # For the header block
    # ------------------------------------------------------------------
    @property
    def address_lines(self) -> list[str]:
        """The address as printable lines, blank ones dropped."""
        return [line.strip() for line in self.address.splitlines() if line.strip()]

    @property
    def contact_line(self) -> str:
        """``"Ph: 021-3456789  ·  sales@example.pk"`` — whichever parts exist."""
        parts = []
        if self.phone:
            parts.append(f"Ph: {self.phone}")
        if self.email:
            parts.append(self.email)
        return "  ·  ".join(parts)

    @property
    def tax_line(self) -> str:
        """``"NTN 1234567-8  ·  STRN 0987654321"`` — whichever parts exist."""
        parts = []
        if self.ntn:
            parts.append(f"NTN {self.ntn}")
        if self.strn:
            parts.append(f"STRN {self.strn}")
        return "  ·  ".join(parts)

    @property
    def is_configured(self) -> bool:
        """Whether anybody has filled this in yet."""
        return bool(self.name)

    def logo_file(self) -> Path | None:
        """The logo's path on disk, or ``None`` when there is nothing to draw.

        Returns ``None`` rather than raising for a row whose file has been moved
        or deleted underneath it. An invoice must print; a missing logo is a
        cosmetic problem and stopping the print is a real one.
        """
        if not self.logo:
            return None
        try:
            path = Path(self.logo.path)
        except (ValueError, NotImplementedError, AttributeError):
            # No filesystem behind the storage backend — nothing local to draw.
            return None
        return path if path.is_file() else None


class ReportAccess(models.Model):
    """A permission holder for the report catalogue. **No table, no rows.**

    The reports are not a model — a Trial Balance is an aggregation over
    :class:`~apps.accounting.models.LedgerEntry` with no row of its own
    (CLAUDE.md §6) — but "may this person open the reports section" and "may
    they see what the business earned" are two real questions, and Django hangs
    a permission on a model or nowhere.

    ``managed = False`` so ``migrate`` creates no table;
    ``default_permissions = ()`` so the four Django would add do not appear,
    because ``add_reportaccess`` would mean nothing and somebody would grant it
    anyway. The ``post_migrate`` hook still creates the two below, which is the
    whole purpose of this class.

    The split is deliberate. A stock balance is an operational question a
    storeman may need; what the owner earned this quarter is not. Which reports
    sit behind which is declared per report — see
    :attr:`apps.reports.registry.Report.permission`.
    """

    class Meta:
        managed = False
        default_permissions = ()
        verbose_name = "report access"
        verbose_name_plural = "report access"
        permissions = [
            ("view_reports", "Can open the reports section"),
            (
                "view_reports_financial",
                "Can open the financial statements: Profit & Loss, Balance Sheet, Trial Balance",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - never instantiated
        return "Report access"
