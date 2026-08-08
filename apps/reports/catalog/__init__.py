"""Every report this system has, in three modules.

Importing this package is what puts the reports on the index — each module calls
:func:`apps.reports.registry.register` at import time. The import happens once,
in :meth:`apps.reports.apps.ReportsConfig.ready`, so a report that is written
and not imported shows up as a missing entry on the index rather than as a URL
that mysteriously 404s.

    accounting.py    the ledger: general ledger, trial balance, P&L, balance
                     sheet, party statements, ageing, day book
    stock.py         the stock ledger: balance, card, item-wise, slow moving
    sales.py         the van: route day sheet, seller and route performance,
                     client-wise sales

The split is by which table the reports read, not by which app the data was
entered in. Nothing in here writes anything.
"""

from . import accounting, sales, stock

__all__ = ["accounting", "sales", "stock"]
