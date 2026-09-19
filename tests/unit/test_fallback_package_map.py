"""Mapping a supplier profile back to the plan that was sold.

The router buys from whichever supplier is cheaper and falls back when one
refuses. A profile that comes back from the fallback still has to find its
plan, or the customer's eSIM is never written — we own it at the supplier and
show nothing. That is what happened to order 145: eSIMCard's wallet was empty,
eSIM Access sold the same tariff as package JC088, and the lookup came back
empty because it read the plan's own eSIMCard code.
"""

from __future__ import annotations

from typing import ClassVar

from app.integrations.esim_access import _plan_by_package_code


class _Offer:
    def __init__(self, provider: str, code: str) -> None:
        self.provider = provider
        self.package_code = code


class _Plan:
    id = 1147
    provider = "esimcard"
    provider_package_code = "85b623c9-uuid"
    offers: ClassVar[list] = [_Offer("esimcard", "85b623c9-uuid"), _Offer("esimaccess", "JC088")]


class _Item:
    plan = _Plan()


class _Order:
    items: ClassVar[list] = [_Item()]


def test_zaxira_provayder_kodi_tarifga_boglanadi():
    # Tarifning o'z provayderi esimcard, lekin xarid eSIM Access'dan bo'lgan.
    assert _plan_by_package_code(_Order()).get("JC088") is not None


def test_tarifning_oz_kodi_yagona_manba_emas():
    # eSIMCard UUID si eSIM Access profilida hech qachon uchramaydi; xaritada
    # bo'lishi zararsiz, lekin u yolg'iz qolsa mos kelish yo'qoladi.
    mapped = _plan_by_package_code(_Order())
    assert set(mapped) >= {"JC088"}, mapped


def test_taklifsiz_eski_tarif_ham_ishlaydi():
    class _Legacy(_Plan):
        provider = "esimaccess"
        provider_package_code = "JC001"
        offers: ClassVar[list] = []

    class _I:
        plan = _Legacy()

    class _O:
        items: ClassVar[list] = [_I()]

    assert _plan_by_package_code(_O()).get("JC001") is not None
