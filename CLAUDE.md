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
  calculation** that produces a stored value.
- 1 rupee = 100 paisa. `settings.PAISA_PER_RUPEE` is the only place that number
  appears.
- `Decimal` is allowed in exactly two places, both at the system boundary:
  parsing operator input (`apps.core.money.to_paisa`) and rendering for display
  or PDF (`apps.core.money.to_rupees`). It never reaches a model field.
- Field names carry the unit: `amount_paisa`, `unit_price_paisa`,
  `discount_paisa`. A field called `amount` is a bug waiting to happen.
- Allocation and proportional splits use `apps.core.money.split_evenly` so the
  parts sum back to the original exactly. Never divide and drop the remainder —
  a lost paisa is an unbalanced ledger.
- Formatting is display only: `{{ value|money }}` in templates,
  `format_money()` in Python.

## 2. Quantity is integer base units. Always.

- Every quantity is a **`BigIntegerField` in base units (pieces)**. Use
  `apps.core.fields.QuantityField`.
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
- **Posted documents are never edited.** To change one: cancel it (which posts
  reversing entries), then create a new document with `amended_from` pointing at
  the cancelled one. The audit trail is the chain of `amended_from` links.
- Never delete a document. Never renumber one.

## 6. Reports read the ledger, never document headers.

- Every report, balance, ageing, stock position and recovery figure is computed
  by **aggregating the ledger and stock tables**.
- **Never** read a cached total off a document header. Do not add
  `total_amount` / `balance` / `stock_on_hand` fields that reports consume.
- A denormalised total may exist **only** as a display convenience on the
  document that owns it, and it is never the source of truth for anything.
- If a report is slow, fix it with an index or a materialised snapshot table
  that is itself derived from the ledger — not by trusting a header field.

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
  config/settings/     base.py + dev.py + prod.py
  apps/
    core/              base models, MoneyField/QuantityField, enums, money helpers
    accounting/        chart of accounts, LedgerEntry (append-only)
    masters/           items, UOM, parties, routes, sellers
    purchasing/        purchase orders, receipts, supplier bills
    sales/             sales orders, invoices, deliveries, returns
    payments/          receipts, payments, recovery
    reports/           read-only aggregation over the ledger
    backup/            SQLite backup/restore for the Windows box
  static/src/          authored + vendored sources (Tailwind input, JS, fonts)
  static/dist/         compiled output — COMMITTED
  templates/
  tests/
  data/                erp.sqlite3 lives here (git-ignored)
```

## Conventions

- App code imports as `apps.<name>`; the Django app label is the bare name
  (`sales`, not `apps.sales`).
- Business logic goes in `apps/<app>/services.py`. Views and admin call
  services; they do not write ledger rows themselves.
- Admin classes use `unfold.admin.ModelAdmin`, not
  `django.contrib.admin.ModelAdmin`.
- Timestamps are timezone-aware (`USE_TZ = True`); use `django.utils.timezone`,
  never `datetime.now()`.
- Tests use pytest and model-bakery. Any new posting service needs a test that
  asserts the ledger balances and that a re-post is rejected.
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
3. No ledger or stock row is updated or deleted anywhere in the diff.
4. Every new posting path is wrapped in `transaction.atomic()`.
5. No new external URL in a template, stylesheet, or script.
6. No new dependency that needs node, a compiler, or a system library.
