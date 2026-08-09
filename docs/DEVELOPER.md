# Working on this codebase

`CLAUDE.md` is the authority. It lists the decisions that must not be broken and
why. This file is the orientation: how the parts fit, what the spine is, and how
to add a document type without snapping it.

Read `CLAUDE.md` first. If something here disagrees with it, `CLAUDE.md` wins and
this file is wrong.

---

## The shape of it

One Django project, one SQLite file, one waitress process on one PC in an office
with no internet. Every architectural decision follows from that sentence.

```
apps/
  core/         money, base models, document codes, the lifecycle contract
  accounts/     groups, permissions, route scope
  accounting/   chart of accounts, LedgerEntry, StockEntry, valuation, year-end
  masters/      items, UOM, clients, vendors, routes, sellers
  purchasing/   supplier bills and returns
  sales/        invoices and returns
  payments/     receipts, allocations, cheques, recovery
  reports/      read-only aggregation, the dashboard, PDFs
  backup/       backup, restore, the backup screen
```

Dependencies run one way: `core` ← `accounts` ← `accounting` ← everything else.
`reports` reads everything and writes nothing.

---

## The spine

Four invariants hold this together. Everything else is detail.

### 1. Money is an integer number of paisa

Never a float, never a `Decimal` in the database. `Decimal` exists at exactly
two boundaries — parsing what an operator typed, and formatting for a screen or
a PDF.

Services do arithmetic with `apps.core.money.Money`, which refuses to add a bare
`int` so that "is this paisa or rupees?" fails loudly instead of silently.

**Rounding happens in one function**, `round_paisa`, and it is banker's rounding.
Half-up is biased away from zero, and on a long run of half-paisa remainders —
which is exactly what percentage discounts produce — that bias accumulates and
the day's sales drift against the ledger. `tests/test_money.py` fails the build
if a second rounding site appears.

### 2. The ledger is append-only

`LedgerEntry` and `StockEntry` are never updated and never deleted. A correction
is a **new row with the opposite sign** that references what it reverses.

`AppendOnlyModel` raises on any save of an existing row and on any delete, and
the managers refuse `bulk_update` and `bulk_create`. Do not reach around it.

This is what makes the audit trail real rather than aspirational: there is no
code path that can rewrite history, so nobody has to be trusted not to.

### 3. Every posting is one transaction

A posting writes a header, its lines, its ledger entries and its stock entries.
All of it lands or none of it does.

Posting lives in a **service function** — not a view, not `save()`, and
emphatically not a signal. Signals are invisible at the call site and make
posting order non-obvious.

```python
@transaction.atomic
def post_sales_invoice(invoice, *, user=None):
    invoice.assert_transition(DocumentStatus.POSTED)
    gl_lines = build_invoice_gl(invoice)
    assert_gl_balances(gl_lines, invoice)  # refuses before writing
    post_entries(invoice, gl_lines, invoice.posting_date, user=user)
    post_stock(...)
    invoice.mark_posted(user=user)
    invoice.save()
    return invoice
```

Debits must equal credits **inside** the transaction, asserted before it commits.

SQLite runs with `transaction_mode: IMMEDIATE`, so the write lock is taken at
`BEGIN`. Keep transactions short — no PDF generation, no network call, no file
I/O inside one.

### 4. Reports read the ledger, never a header

Every balance, ageing figure and stock position is aggregated from
`LedgerEntry` / `StockEntry`. There is no `total_amount` column that a report
consumes, and adding one is the change most likely to be quietly wrong for a
year.

A denormalised total may exist as a display convenience on the document that
owns it. It is never the source of truth for anything.

---

## The document lifecycle

`DRAFT → POSTED → CANCELLED`. Those are the only two transitions.

- **DRAFT** — editable, has written nothing. May be deleted.
- **POSTED** — immutable, has ledger and stock rows.
- **CANCELLED** — immutable, has reversing rows as well.

`DocumentModel.save()` enforces immutability rather than trusting callers:
changing any field on a POSTED row raises `DocumentImmutable` naming the code.

**Amending** requires the document to be CANCELLED first — amending a POSTED
document would double-count it, since nothing has been reversed yet. The chain
is **linear**: `SI-2026-000123` → `-1` → `-2`, never two `-1`s. Amending the same
cancelled document twice is refused, and it is refused because a property test
found the fork and the `IntegrityError` it produced.

---

## Adding a document type

Follow this and the shared screens, the cancel flow, the timeline, the reports
and the integrity check all work without being told about you.

**1. The model.** Inherit `DocumentModel`, declare a `CODE_PREFIX`, and add the
three lifecycle permissions:

```python
class DeliveryNote(DocumentModel):
    CODE_PREFIX = "DN"

    class Meta:
        permissions = [
            ("post_deliverynote", "Can post a delivery note"),
            ("cancel_deliverynote", "Can cancel a delivery note"),
            ("amend_deliverynote", "Can amend a cancelled delivery note"),
        ]
```

**2. The three methods**, with exactly these signatures — a caller holding a
document it knows nothing about still has to be able to act on it:

```python
def post(self, *, user=None, **options): ...
def cancel(self, *, user=None, reason: str = ""): ...
def amend(self, *, user=None): ...
```

Each delegates to a service in `services.py`. `post` and `cancel` are wrapped in
`transaction.atomic()`.

**3. `dependents()`** — what would be left dangling if this were reversed. It
**must never raise**, including on an unsaved instance, because the shared cancel
screen calls it before it knows anything about the document.

**4. `get_absolute_url()`** — the shared timeline and cancel templates link
through it.

**5. The code**, from `get_next_code(prefix, fiscal_year)`, called **inside** the
same `atomic()` block that saves the document so a failed save does not burn a
number. Never build a code by hand.

**6. Cancelling** goes through `apps.core.views.cancel_view`. Do not write a
second cancel screen.

`tests/test_lifecycle.py::TestTheContract` discovers every `DocumentModel`
subclass automatically and holds all of them to the above. You will find out
immediately if you missed one — that is what it is for.

---

## Testing

```
make test                     # the suite
make lint                     # ruff check + format check
```

Roughly 1,700 tests. The ones that matter most:

| File | Guards |
| ---- | ------ |
| `test_invariants_property.py` | Hypothesis: random post/cancel/amend sequences, invariants after **every** step |
| `test_lifecycle.py` | The document contract, applied to every document type |
| `test_concurrency.py` | Ten simultaneous postings: no duplicate codes, no lock errors |
| `test_sequences.py` | Real threads racing for a document number |
| `test_reports.py` | The trial balance sums to zero across posted, cancelled and amended |
| `test_money.py` | One rounding site; no float ever reaches a stored value |
| `test_performance.py` | Query counts stay constant as rows grow |
| `test_backup.py` | The full backup → wipe → restore round trip |

### The property test

`tests/test_invariants_property.py` generates random sequences of
post / cancel / amend / receive and re-checks four invariants after every single
step: the trial balance is zero, every voucher balances on its own, stock equals
the sum of its entries, and a cancelled document nets to exactly zero on every
account it touched.

It found the amendment-fork bug described above. Run it harder when touching
posting:

```
ERP_PROPERTY_EXAMPLES=2000 pytest tests/test_invariants_property.py
```

Business refusals are counted, not failed — and tagged as Hypothesis events, so
`--hypothesis-show-statistics` shows whether a run actually posted anything or
passed by refusing everything.

---

## Performance

The cost of the recovery workspace, the ageing ladder and the dashboard scales
with the number of **open items**, not with the size of the ledger. Settled bills
are cheap; unsettled ones are examined individually.

Measured against 50,000 invoices and 285,000 ledger entries:

| Open items | dashboard | ageing | recovery |
| ---------- | --------- | ------ | -------- |
| 7,500 | 155 ms | 142 ms | 267 ms |
| 15,000 | 286 ms | 238 ms | 451 ms |
| 50,000 | 1,566 ms | 1,097 ms | 2,260 ms |

A business writing 50,000 invoices a year and collecting most of them sits in the
first row. The last row is a business that has not been paid all year — worth
knowing about, not worth optimising for today. If an installation ever
approaches it, the fix is to bound the recovery computation by date or to
materialise open items into a snapshot table derived from the ledger; do **not**
add a balance column to the invoice header.

`ledger_party_voucher_idx` is a covering index for that GROUP BY: it took the
query from 178 ms to 94 ms at 200,000 entries by removing the temp B-tree.
`tests/test_performance.py` asserts the query plan still uses it.

To profile:

```
python manage.py migrate     --settings=config.settings.profile
python manage.py seed_volume --invoices 50000 --settings=config.settings.profile
python scripts/profile_pages.py
```

`seed_volume` writes to `data/profile.sqlite3`, never the development database,
and bypasses the posting services on purpose — it is measuring the read path.
Never quote a *posting* benchmark from it.

---

## Conventions

- App code imports as `apps.<name>`; the app label is bare (`sales`).
- Business logic in `services.py`. Views call services; they never write ledger
  rows themselves.
- Admin classes use `unfold.admin.ModelAdmin`.
- Timestamps are timezone-aware; use `django.utils.timezone`.
- Templates load display filters with `{% load core_tags %}`.
- Lint with ruff, line length 100.

### Errors

`CoreError` and its subclasses are **business refusals** — over the credit limit,
short of stock, a document with dependents. Views catch them and put the message
beside the field. They are written for an operator, so they say what to do.

Anything else is a bug and reaches `apps/core/errors.py`, which logs the
traceback with a short reference and shows the operator a page carrying that
same reference. Never let a traceback reach a screen.

### The front end

No node, no npm, no CDN. Tailwind is compiled by the standalone binary and the
output is **committed** to `static/dist`. htmx and Alpine are vendored with the
version in the filename. Fonts are self-hosted.

`tests/test_project_setup.py::TestNoExternalAssets` fails the build if a CDN URL
appears anywhere.

---

## Things that look wrong and are not

- **`select_for_update()` on SQLite** is a no-op. It is kept so the code stays
  correct on a row-locking backend; what actually serialises two simultaneous
  invoices is `BEGIN IMMEDIATE`.
- **Two print paths.** `@media print` is the fast one the counter uses all day;
  ReportLab is the file that gets emailed. A screen that prints badly is a bug
  even when its PDF is perfect.
- **Gaps in document numbers** are normal and are never a reason to renumber.
- **`django-simple-history` on masters only.** Documents are not registered: a
  posted one cannot change, and every correction is already a reversing entry
  under its own date and user.
- **The dashboard cache** holds figures for 60 seconds. Nothing financial is
  served from it that anybody acts on — every report re-aggregates.
