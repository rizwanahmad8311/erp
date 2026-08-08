# CLAUDE.md — locked decisions for this project

**Read this file at the start of every session, before writing any code.**

These are not preferences. They are load-bearing decisions about a system that
records money movement for a real distribution business. Breaking one of them
silently corrupts financial history in a way that is expensive or impossible to
unwind. If a requirement seems to need one of these rules broken, stop and ask
— do not work around it.

---

## 1. Money is integer paisa. Always.

- Every monetary value in the database is a **`BigIntegerField` holding paisa**.
  Use `apps.core.fields.MoneyField`.
- **Never `FloatField`. Never `DecimalField`. Never a Python `float` in any
  calculation** that produces a stored value. `to_paisa()` rejects `float`
  outright — pass a string or a `Decimal`.
- 1 rupee = 100 paisa. `settings.PAISA_PER_RUPEE` is the only place that number
  appears.
- `Decimal` is allowed in exactly two places, both at the system boundary:
  parsing operator input (`apps.core.money.to_paisa`) and rendering for display
  or PDF (`to_rupees`, `fmt`). It never reaches a model field.
- Field names carry the unit: `amount_paisa`, `unit_price_paisa`,
  `discount_paisa`. A field called `amount` is a bug waiting to happen.

### Rounding — banker's, in exactly one place

- **`apps.core.money.round_paisa()` is the only rounding point in the system.**
  It uses `ROUND_HALF_EVEN`: an exact half goes to the nearest *even* paisa, so
  0.5 → 0, 1.5 → 2, 2.5 → 2.
- Half-up is biased away from zero. On a long run of half-paisa remainders —
  which is exactly what percentage discounts and per-line tax produce — that
  bias accumulates in one direction and the day's sales drift against the
  ledger. Half-even splits the halves both ways and the error cancels.
- **Never write `round()` or `.quantize()` on a monetary value anywhere else.**
  `to_paisa`, `Money.__mul__`, `Money.percent` and every service all funnel
  through `round_paisa`. `tests/test_money.py` fails the build if a second
  rounding site appears under `apps/`.

### Arithmetic

- Services do money arithmetic with **`apps.core.money.Money`**, an immutable
  value object wrapping integer paisa. Wrap at the top of the service, unwrap
  with `.paisa` when writing rows back.
- `Money + int` raises on purpose — that is the guard that catches
  "is this paisa or rupees?" bugs.
- Model fields still store and return plain `int`. `MoneyField` deliberately
  does not convert to `Money`: a field returning a custom object breaks
  aggregates, `F()` expressions and `values_list()`.
- Allocation and proportional splits use `Money.allocate(weights)` or
  `Money.split(n)`, whose parts sum back to the original **exactly**. Never
  divide and drop the remainder — a lost paisa is an unbalanced ledger.
- Formatting is display only: `{{ value|money }}` in templates, `fmt()` in
  Python, `format_money()` when a currency symbol is wanted inline.

## 2. Quantity is integer base units. Always.

- Every quantity is an **`IntegerField` in base units (pieces)**. Use
  `apps.core.fields.QuantityField`.
- 32-bit, unlike `MoneyField`. Piece counts for a distribution business stay far
  below two billion, and the narrower column makes a paisa value accidentally
  assigned to a quantity field much more likely to fail loudly.
- **Never fractional.** There is no 2.5 pieces.
- Packaging is a **UOM conversion on the item**, not a fractional quantity. A
  carton of 12 is `qty_pieces = 12`, with the carton as a display/entry unit
  that multiplies on input and divides for presentation.
- Field names carry the unit: `qty_pieces`, `free_qty_pieces`.

## 3. LedgerEntry and StockEntry are APPEND-ONLY.

- **Never `UPDATE`. Never `DELETE`.** No exceptions, not for a typo, not for a
  test fixture, not for a data migration.
- Corrections are made by **writing reversing rows** — a new entry with the
  opposite sign that references what it reverses.
- Both models inherit `apps.core.models.AppendOnlyModel`, which raises
  `AppendOnlyViolation` on any save of an existing row and on any delete. Do not
  remove that guard, and do not reach around it with `.update()`,
  `bulk_update()`, or raw SQL.
- The same rule applies to management commands, admin actions, and shell
  sessions. The admin registers these models read-only.

## 4. Every posting runs inside `transaction.atomic()`.

- A posting writes a document header, its lines, its ledger entries and its
  stock entries. Either **all** of it lands or **none** of it does.
- Posting logic lives in a **service function**, not in a view, not in a model
  `save()`, not in a signal. Signals are invisible at the call site and make
  posting order non-obvious — do not use them for financial writes.
- Shape of every posting service:

  ```python
  from django.db import transaction


  @transaction.atomic
  def post_sales_invoice(invoice, *, user):
      invoice.assert_transition(DocumentStatus.POSTED)
      ...  # write ledger entries
      ...  # write stock entries
      invoice.status = DocumentStatus.POSTED
      invoice.posted_at = timezone.now()
      invoice.save(update_fields=["status", "posted_at", "updated_at"])
  ```

- Debits must equal credits **within the same transaction**, asserted before it
  commits.
- SQLite is configured with `transaction_mode: IMMEDIATE`, so the write lock is
  taken at `BEGIN`. Keep transactions short: never do I/O, PDF generation, or a
  network call inside one.

## 5. Document lifecycle: DRAFT → POSTED → CANCELLED

- Every document inherits `apps.core.models.DocumentModel`.
- **`DRAFT`** — editable, has written nothing to any ledger.
- **`POSTED`** — **immutable**, has ledger and stock rows.
- **`CANCELLED`** — **immutable**, has reversing ledger and stock rows.
- Those are the only two legal transitions. See
  `apps.core.enums.ALLOWED_STATUS_TRANSITIONS`; call `assert_transition()`
  before changing status.
- **Posted documents are never edited**, and `DocumentModel.save()` enforces it
  rather than trusting callers: changing any field on a POSTED or CANCELLED row
  raises `DocumentImmutable` naming the document code. The only fields a
  cancellation may write are listed in `DocumentModel.CANCELLATION_FIELDS`.
- Subclasses implement `post()`, `cancel()` and `amend()`; the base raises
  `NotImplementedError` for all three. `post()` and `cancel()` must be wrapped
  in `transaction.atomic()`. The signatures are fixed and identical for every
  document type — `post(*, user=None, **options)`,
  `cancel(*, user=None, reason="")`, `amend(*, user=None)` — so a caller holding
  a document it knows nothing about can still act on it.
  `tests/test_lifecycle.py::TestTheContract` fails the build if one drifts.
- **Every `cancel_*` calls `assert_cancellable()` before it writes.** A document
  whose reversal would leave something else dangling — a payment allocated to
  it, a credit note raised against it, a cheque event on it — refuses and names
  what blocks it. Each type answers this by implementing `dependents()`.
- Cancelling from the UI goes through `apps.core.views.cancel_view`: the
  `<app>.cancel_<model>` permission, a typed reason of at least
  `MIN_CANCEL_REASON` characters, and a preview of the exact reversing entries
  shown *before* the button. Do not add a second cancel screen.
- Cancelled documents are **never hidden from a list view and never deleted**.
  They are watermarked on screen and on paper, and left out of *figures* by
  `objects.live()`, with `?include_cancelled=1` as the explicit audit toggle.
- **Amending**: a document must be CANCELLED before it can be amended — amending
  a POSTED document would double-count it, since nothing has been reversed yet.
  `build_amendment()` clones the header into a new DRAFT with `amended_from`
  set and `amendment_no` incremented; the subclass copies its own lines on.
- Amendment codes suffix the **root** code, so a chain reads
  `SI-2026-000123` → `-1` → `-2`, never `-1-1`. The suffix comes from
  `root_document()`, not from string-munging the current code — `SI-2026-000123`
  already ends in digits. `chain()` returns the whole lineage oldest-first from
  any link in it, and `timeline()` is what every detail page renders.
- **Never renumber a document.** A DRAFT may be deleted (it has written nothing
  to any ledger and has no reversing entries to lose); a POSTED or CANCELLED
  document may not, and `delete()` raises `DocumentImmutable` if you try.

### Document codes

- Format `PREFIX-YYYY-NNNNNN`, allocated only by
  `apps.core.services.get_next_code(prefix, fiscal_year)`.
- Call it **inside** the same `atomic()` block that saves the document, so a
  failed save does not burn a number.
- Gaps in the sequence are normal and are never a reason to renumber.
- Numbering restarts each fiscal year and is independent per prefix.
- On SQLite, `select_for_update()` is a no-op — what actually serialises two
  simultaneous invoices is `transaction_mode: IMMEDIATE` taking the write lock
  at `BEGIN`. The `select_for_update()` call is kept so the code stays correct
  on a row-locking backend. Never "optimise" either one away.
- Never edit `DocumentSequence.last_number` by hand. The admin is read-only for
  exactly this reason.

## 6. Reports read the ledger, never document headers.

- Every report, balance, ageing, stock position and recovery figure is computed
  by **aggregating the ledger and stock tables**.
- **Never** read a cached total off a document header. Do not add
  `total_amount` / `balance` / `stock_on_hand` fields that reports consume.
- A denormalised total may exist **only** as a display convenience on the
  document that owns it, and it is never the source of truth for anything.
- If a report is slow, fix it with an index or a materialised snapshot table
  that is itself derived from the ledger — not by trusting a header field.
- A report that reads **documents** rather than the ledger reads
  `Model.objects.live()`, which leaves cancelled ones out. Never filter them out
  of a *list* screen: a cancelled document is the correction somebody is looking
  for, and hiding it is the opposite of an audit trail.

## 6a. History is for masters. The ledger is the documents' history.

- `django-simple-history` is registered on **`Item`, `Client`, `Vendor`,
  `Route`, `Seller` and `Account`** and on nothing else. A master is corrected
  in place, so without a history table "who raised this shop's credit limit"
  has no answer.
- **Never register a document.** A POSTED document cannot be modified at all
  (§5) and every correction is already a reversing entry in the ledger under its
  own date and its own user (§3). A second audit log over documents is a second
  version of the truth, and the two eventually disagree.
- Never register `LedgerEntry` or `StockEntry`: a row that can never change has
  no history to keep.
- `tests/test_lifecycle.py::TestMasterHistory` fails the build if either rule is
  broken.

## 6b. Printing: ReportLab, and two paths to paper.

- PDFs are drawn by **ReportLab** in `apps/reports/pdf/`. **Never WeasyPrint,
  xhtml2pdf, wkhtmltopdf or headless Chrome** — each needs a system library or a
  binary that turns the six-line install in §8 into a support call on a machine
  with no internet and nobody sitting at it. ReportLab is a pure-Python wheel and
  its one compiled dependency (Pillow) ships a prebuilt Windows wheel.
- **There are two output paths and they are not redundant.** `@media print` in
  `static/src/css/app.css` is the fast one — Ctrl+P on the screen, no PDF step —
  and it is what the counter uses all day. `?format=pdf` is the file that gets
  emailed, filed or handed over. A screen that prints badly is a bug even when
  its PDF is perfect.
- Because the two are written twice, they read the **same** `CompanyProfile` row
  and the same `|words` filter, so the details cannot disagree even though the
  layouts can.
- **Never generate a PDF inside a posting transaction** (§4). Rendering is I/O
  and the SQLite write lock is taken at `BEGIN`.
- Amounts print through `apps.core.words.amount_in_words`, which uses **lakh and
  crore**. "Ten Million" on a bill in Karachi is a bill that gets queried.
- The company logo is **uploaded and stored under `MEDIA_ROOT`, never
  hotlinked** (§7). A logo that cannot be read is skipped with a log line — an
  invoice must always print.

## 7. No CDN references. Anywhere.

- **The production machine has no internet.** A single `<script src="https://…">`
  means a broken page in the office.
- All CSS, JS and fonts are **vendored into `static/src` and committed compiled
  into `static/dist`**. `static/dist` is deliberately **not** git-ignored.
- No `<link>` or `<script>` pointing at an external host. No `@import url(...)`
  to a font service. No `pip install` at runtime.
- Adding a JS library means downloading it, committing it under
  `static/src/js/vendor/` with the version in the filename, and recording it in
  that directory's README.
- `tests/test_project_setup.py::TestNoExternalAssets` fails the build if a CDN
  URL appears in a template or asset.

## 8. Windows production needs only Python + pip.

The deployment story is, in full:

```
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
python serve.py
```

Therefore:

- **No node, no npm, no Docker, no system libraries, no compiler.**
- Every dependency must be a **pure-Python wheel**. Before adding a package,
  check that it has no build step and no binary extension. If it needs a C
  compiler or a DLL, it does not go in `requirements.txt`.
- Docker exists for **macOS development only**. Nothing in production may
  depend on a Dockerfile, a Makefile target, or anything in `docker/`.
- The Makefile is a dev convenience. It must never become part of deployment.
- Static assets are already built and committed; production only runs
  `collectstatic`, which copies files.

---

## Layout

```
erp/
  manage.py            dev entrypoint (defaults to config.settings.dev)
  serve.py             prod entrypoint (waitress)
  config/settings/     base.py + dev.py + prod.py + test.py (pytest only)
  apps/
    core/              money primitives, base models, document codes, filters
    accounting/        chart of accounts, LedgerEntry (append-only)
    masters/           items, UOM, parties, routes, sellers
    purchasing/        purchase orders, receipts, supplier bills
    sales/             sales orders, invoices, deliveries, returns
    payments/          receipts, payments, recovery
    reports/           read-only aggregation over the ledger
      catalog/         the reports themselves — accounting, stock, sales
      pdf/             ReportLab renderers — invoices, receipts, statements
    backup/            SQLite backup/restore for the Windows box
  static/src/          authored + vendored sources (Tailwind input, JS, fonts)
  static/dist/         compiled output — COMMITTED
  templates/
  tests/
    testapp/           concrete models for testing the abstract bases (pytest only)
  data/                erp.sqlite3 lives here (git-ignored)
  media/               the uploaded company logo (git-ignored)
```

### What apps/reports provides

A report is **a column list and a function**, and nothing else — no view, no
template, no URL entry. Those are shared once, which is what makes the HTML
table, the CSV and the PDF the same columns in the same order by construction
rather than by three people remembering.

| Module | Contents |
| ------ | -------- |
| `columns.py` | `Column`, `ReportRow`, and the two renderings of a cell: `display` for a human, `export` for a spreadsheet |
| `criteria.py` | `Criteria`, `ReportFilterForm` — the filter bar every report shares, including the cancelled toggle |
| `registry.py` | `Report`, `ReportResult`, `register` — the catalogue, built at import time |
| `ledger.py` | the aggregation primitives: `account_totals`, `party_totals`, `voucher_totals`, `stock_positions`, `voucher_targets`. **The only module here that touches `LedgerEntry`** |
| `framework.py` | `ReportView` — one view, three formats, pagination, the index |
| `exports.py` | the CSV writer, with the formula-injection guard |
| `catalog/` | the eighteen reports, grouped by which table they read |
| `pdf/reports.py` | the generic report PDF: any registered report, landscape-aware |

Adding a report means writing one `build(criteria) -> ReportResult` and calling
`register`. It appears on the index, gets its own URL, and answers `?format=csv`
and `?format=pdf` without anything else being touched.

Two invariants the tests fail the build over:

- **the Trial Balance sums to zero** over a dataset containing posted, cancelled
  and amended documents, and prints the difference in the alarm colour when it
  does not (`tests/test_reports.py::TestTrialBalance`);
- **"recovery" means one thing** — `ledger.RECOVERY_VOUCHERS`, which nets a
  bounced cheque off in the period it bounced. A bounce does not reverse its
  receipt (§5), so a figure summed over payments alone counts money that never
  arrived.

### What apps/reports/pdf provides

| Module | Contents |
| ------ | -------- |
| `documents.py` | `sales_invoice_pdf`, `purchase_invoice_pdf` |
| `receipts.py` | `payment_receipt_pdf` — A4/A5 sheet, or an 80mm/58mm till roll |
| `ledgers.py` | `client_ledger_pdf`, `route_day_sheet_pdf` |
| `base.py` | `PDFDocument` (repeating letterhead, numbered footer), `ThermalDocument` |
| `blocks.py` | letterhead, line table, totals, amount in words, signature |
| `canvas.py` | `NumberedCanvas` — "page x of y" and the CANCELLED watermark |
| `fonts.py` | registers vendored TTFs from `static/src/fonts/`; falls back to built-ins |
| `theme.py` | paper sizes, the palette converted from the CSS tokens, paragraph styles |

### What apps/core provides

| Module | Contents |
| ------ | -------- |
| `money.py` | `to_paisa`, `to_rupees`, `fmt`, `format_money`, `round_paisa`, `Money` |
| `fields.py` | `MoneyField` (paisa, 64-bit), `QuantityField` (pieces, 32-bit) |
| `models.py` | `TimeStampedModel`, `AppendOnlyModel`, `DocumentModel`, `DocumentSequence` |
| `services.py` | `get_next_code`, `peek_next_code` |
| `enums.py` | `DocumentStatus`, `ALLOWED_STATUS_TRANSITIONS` |
| `exceptions.py` | `DocumentImmutable`, `IllegalTransition`, `AppendOnlyViolation`, `DocumentHasDependents`, `PaymentAllocated`, `MoneyError`, `SequenceError` |
| `lifecycle.py` | `Dependent`, `TimelineStep`, `document_timeline`, `payment_allocations`, `payment_dependents` |
| `words.py` | `amount_in_words`, `number_in_words` (+ Urdu) — lakh and crore, never million |
| `reporting.py` | `DocumentQuerySet` (`live` / `cancelled` / `for_report`), `include_cancelled_from` |
| `forms.py` | `CancelForm`, `MIN_CANCEL_REASON` |
| `views.py` | `cancel_view` — the cancel screen every app shares |
| `templatetags/core_tags.py` | `\|money`, `\|qty`, `\|words`, `\|doc_status` |

## Conventions

- App code imports as `apps.<name>`; the Django app label is the bare name
  (`sales`, not `apps.sales`).
- Business logic goes in `apps/<app>/services.py`. Views and admin call
  services; they do not write ledger rows themselves.
- Admin classes use `unfold.admin.ModelAdmin`, not
  `django.contrib.admin.ModelAdmin`.
- Timestamps are timezone-aware (`USE_TZ = True`); use `django.utils.timezone`,
  never `datetime.now()`.
- Templates load display filters with `{% load core_tags %}`.
- Tests use pytest and model-bakery, and run under `config.settings.test`, which
  installs `tests.testapp` and puts the test database on disk so the threaded
  concurrency tests exercise real SQLite locking. Any new posting service needs a
  test that asserts the ledger balances and that a re-post is rejected.
- Lint with `ruff` (`make lint`, `make fmt`). Line length 100.

## Commands

| Task | Command |
| ---- | ------- |
| Start dev server | `make up` → http://localhost:8000/admin/ |
| Stop | `make down` |
| Django shell | `make shell` |
| Tests | `make test` |
| Lint / format | `make lint` / `make fmt` |
| Migrations | `make makemigrations` then `make migrate` |
| Rebuild CSS | `make css` (then **commit `static/dist`**) |
| Prod readiness check | `make check` |

## Before you finish any task

1. `make lint` and `make test` both pass.
2. No `DecimalField`, `FloatField`, or fractional quantity was introduced.
3. No rounding outside `money.round_paisa` — no stray `round()` or `.quantize()`.
4. No ledger or stock row is updated or deleted anywhere in the diff.
5. Every new posting path is wrapped in `transaction.atomic()`.
6. Every new document code came from `get_next_code`, never a hand-built string.
7. No new external URL in a template, stylesheet, or script.
8. No new dependency that needs node, a compiler, or a system library.
