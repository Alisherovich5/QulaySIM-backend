"""Placing the same top-up twice must return one order, not two.

The cart endpoint has taken an `Idempotency-Key` since it was written; the
top-up endpoint never did. The live database shows what that cost: eSIM 69
collected three orders in 104 seconds, two of them abandoned, and 53 of 93
orders sit unpaid.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services import topup_orders

pytestmark = pytest.mark.anyio


class _Order:
    def __init__(self, oid=77, amount=Decimal("46999"), rate=Decimal("12500")):
        self.id = oid
        self.total = Decimal("3.99")
        self.amount_uzs = amount
        self.exchange_rate = rate


class _Plan:
    title = "Thailand 3GB 15Days (qo'shimcha)"


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Session:
    """Records what was asked and what was written."""

    def __init__(self, row):
        self._row = row
        self.statements = []
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _Result(self._row)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        pass


async def _place(monkeypatch, session, esim_id=75, code="TOPUP_JC013"):
    class _ESIM:
        id = esim_id

    async def _owned(_s, _c, _i):
        return _ESIM()

    monkeypatch.setattr(topup_orders, "owned_esim", _owned)
    monkeypatch.setattr(topup_orders.topup_service, "is_toppable", lambda _e: True)

    async def _url(order_id, amount, lines):
        return f"https://pay.example/{order_id}"

    monkeypatch.setattr(topup_orders, "_payment_url_for_lines", _url)

    class _Customer:
        id = 98

    return await topup_orders.place(session, _Customer(), esim_id, code)


async def test_ikkinchi_urinish_ayni_buyurtmani_qaytaradi(monkeypatch):
    session = _Session((_Order(oid=77), _Plan()))
    placed = await _place(monkeypatch, session)

    assert placed["order_id"] == 77, placed
    assert placed["amount_uzs"] == Decimal("46999")
    # Eng muhimi: yangi buyurtma YOZILMAGAN bo'lishi kerak.
    assert session.added == [], f"yangi qator yozildi: {session.added}"
    assert session.committed is False


async def test_qidiruv_aynan_shu_esim_paket_va_pending_boyicha(monkeypatch):
    session = _Session((_Order(), _Plan()))
    await _place(monkeypatch, session, esim_id=75, code="TOPUP_JC013")

    assert session.statements, "hech qanday so'rov yuborilmadi"
    compiled = session.statements[0].compile()
    params = compiled.params
    values = {str(v) for v in params.values()}
    assert "75" in values, params           # aynan shu eSIM
    assert "TOPUP_JC013" in values, params  # aynan shu paket
    assert "98" in values, params           # aynan shu mijoz
    assert any("pending" in str(v).lower() for v in params.values()), params
    # oyna: 15 daqiqadan eskisi olinmasin
    stamps = [v for v in params.values() if isinstance(v, datetime)]
    assert stamps, params
    age = datetime.now(UTC) - stamps[0]
    assert timedelta(minutes=14) < age < timedelta(minutes=16), age


async def test_som_summasi_yoq_buyurtma_qayta_ishlatilmaydi(monkeypatch):
    # To'lab bo'lmaydigan qatorni qaytarish mijozni o'lik havolaga olib boradi.
    session = _Session((_Order(amount=None), _Plan()))
    reused = await topup_orders._reusable_order(session, type("C", (), {"id": 98})(), 75, "X")
    assert reused is None
