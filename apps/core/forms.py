"""Forms shared by every document screen.

There is exactly one thing here today and it is here rather than in each app for
the reason everything else in :mod:`apps.core` is: three copies of "why are you
cancelling this" would be three different minimum lengths within a year.
"""

from __future__ import annotations

from django import forms

#: How much of a reason is a reason. Ten characters is not a magic number — it
#: is the shortest thing that cannot be "ok", "typo", "wrong" or a full stop,
#: and a cancellation is a reversing entry in a real ledger that somebody will
#: be asked about in a year. It is enforced here rather than in the service
#: because it is a policy about *operators*, not about the books: a management
#: command correcting data has no keyboard to type into.
MIN_CANCEL_REASON = 10


class CancelForm(forms.Form):
    """The typed reason a cancellation is confirmed with.

    Rendered on the confirmation screen beside the reversing entries the
    cancellation would write, so the reason is typed by somebody who has just
    read what is about to happen.
    """

    reason = forms.CharField(
        label="Why is this being cancelled?",
        min_length=MIN_CANCEL_REASON,
        max_length=500,
        strip=True,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "autofocus": "autofocus",
                "placeholder": "e.g. Quantity on line 2 was wrong — supplier sent 8 cartons, not 10",
            }
        ),
        error_messages={
            "required": "A cancellation needs a reason. It is written onto the document forever.",
            "min_length": (
                f"Say a little more — at least {MIN_CANCEL_REASON} characters. "
                f"This is what somebody reads in a year when they ask why the entries "
                f"were reversed."
            ),
        },
    )


__all__ = ["MIN_CANCEL_REASON", "CancelForm"]
