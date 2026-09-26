"""What to call a customer on a screen.

One place, because there were four: `full_name or email.split("@")[0]`, written
out at each call site. That expression carried an assumption the column no
longer makes — an account opened with Telegram has no address to take a name
from — and four copies is four chances to fix it in three of them.
"""

from __future__ import annotations

from typing import Protocol


class Named(Protocol):
    full_name: str
    email: str | None


def display_name(person: Named, *, fallback: str = "Mijoz") -> str:
    """The best name we have, and never an empty string.

    In order: what they told us, then the local part of their address, then a
    word. The last step is what stops a Telegram account with no name and no
    address rendering as a blank cell that reads like a bug.
    """
    name = (person.full_name or "").strip()
    if name:
        return name
    email = (person.email or "").strip()
    if email:
        return email.split("@", 1)[0]
    return fallback
