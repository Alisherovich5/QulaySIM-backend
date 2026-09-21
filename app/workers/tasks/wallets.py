"""The wallet watch: tell somebody before the money runs out, not after.

Two jobs, and the order matters. It records what each wholesaler's wallet holds
so checkout can refuse a plan nobody can pay for (see
`app/integrations/wallets.py`), and it tells the owner while there is still
time to top up.

That second job is the one that was missing. Order #141 was not a mystery to the
system: the supplier said "Insufficient Wallet Balance", the rescue logged it,
the backoffice drew the wallet card in red. Nothing said it out loud until a
customer had already paid, and by then the only remaining moves were a top-up
hours late or a refund. An empty wallet is visible for days before it costs
anything — the failure here was one of announcement, not of detection.

The threshold is a number of dollars rather than a number of days of trading:
days-of-cover is the nicer measure and it is what the dashboard shows, but it
needs yesterday's spend to mean anything, and a watch that goes quiet on a slow
week is a watch that goes quiet exactly when nobody is looking at the dashboard
either.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.workers.celery_app import celery_app

logger = get_logger("wallets")

#: Below this, a wallet is one busy afternoon from empty. Chosen against the
#: catalogue rather than by feel: wholesale costs run to roughly $35 for the
#: largest plans, so this is several of the worst case and dozens of the usual
#: one — enough room that a top-up can happen during working hours.
LOW_WALLET_USD = 40.0

#: How long a warned wallet stays quiet. Shorter than the rescue's day, because
#: this alert is asking for an action that takes minutes and it should still be
#: on the phone's screen that evening; long enough that a wallet sitting just
#: under the line does not send four messages before lunch.
ALERT_SILENCE = 6 * 60 * 60


@celery_app.task(name="maintenance.check_wallets")
def check_wallets() -> dict[str, float | None]:
    """Record every wholesaler's balance, and warn about the thin ones."""
    from app.integrations.wallets import fetch_balances, record

    balances = fetch_balances()
    record(balances)

    low = sorted(
        (key, value)
        for key, value in balances.items()
        if value is not None and value < LOW_WALLET_USD
    )
    if low:
        logger.warning("wallets.low", wallets=dict(low))
        _alert(low)

    unreachable = [key for key, value in balances.items() if value is None]
    if unreachable:
        # Not alerted on: a wholesaler's balance endpoint times out often enough
        # that a message about it would train the reader to ignore messages.
        # Checkout already treats an unknown balance as "sell", so the shop
        # stays open and the rescue stays underneath it.
        logger.warning("wallets.unreachable", providers=unreachable)

    return balances


def _alert(low: list[tuple[str, float]]) -> None:
    """Tell the owner which wallet is thin, and what it stops."""
    import redis

    from app.core.config import settings
    from app.integrations.telegram import send_html_blocking

    fresh: list[tuple[str, float]] = []
    try:
        client = redis.Redis.from_url(str(settings.redis_url))
        for provider, balance in low:
            if client.set(f"wallets:alerted:{provider}", b"1", ex=ALERT_SILENCE, nx=True):
                fresh.append((provider, balance))
    except Exception:  # noqa: BLE001 - a duplicate warning beats a silent one
        logger.warning("wallets.alert_dedupe_unavailable")
        fresh = low

    if not fresh:
        return

    lines = ["<b>💳 Ta'minotchi hisobi tugayapti</b>"]
    for provider, balance in fresh:
        lines.append(f"{provider}: <b>${balance:.2f}</b>")
    lines.append(
        "Hisobni to'ldiring. Aks holda faqat shu ta'minotchida bor tariflar "
        "sotuvdan vaqtincha olinadi — pul olinib, eSIM berilmasligidan shu himoya qiladi."
    )

    try:
        send_html_blocking("\n".join(lines))
    except Exception:
        logger.exception("wallets.alert_failed")
