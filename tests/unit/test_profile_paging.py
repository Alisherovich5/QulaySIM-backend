"""Ta'minotchidan BARCHA profillarni olish.

Ilgari faqat 1-sahifa (50 ta) so'ralardi. 17 ta profil bilan bu sezilmaydi.
50 dan oshgan kunda eng eskilari ro'yxatdan tushib qolar va traffigi jimgina
yangilanmay qolardi -- ya'ni "qancha qolganini ko'rsatmayapti" degan shikoyat
aynan shu sababdan kelib chiqar, va sabab hech qayerda ko'rinmasdi.
"""

from __future__ import annotations

from typing import Any

from app.integrations.esim_access import EsimAccessClient


class Recorder(EsimAccessClient):
    """Haqiqiy `query_profiles` ni ishlatadi, faqat tarmoqni almashtiradi."""

    def __init__(self, pages: list[list[dict[str, Any]]]):
        self._pages = pages
        self.asked: list[int] = []

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:  # type: ignore[override]
        page = int(payload["pager"]["pageNum"])
        self.asked.append(page)
        items = self._pages[page - 1] if page - 1 < len(self._pages) else []
        return {"success": True, "obj": {"esimList": items, "note": "keep"}}


def _profiles(count: int, start: int = 0) -> list[dict[str, Any]]:
    return [{"esimTranNo": f"tran-{start + i}"} for i in range(count)]


def test_a_single_short_page_is_one_request() -> None:
    """Bugungi holat: 17 profil, bitta so'rov. Sahifalash qo'shilgani bilan
    ortiqcha chaqiruv paydo bo'lmasligi kerak."""

    client = Recorder([_profiles(17)])

    result = client.query_profiles(order_no="")

    assert len(result["obj"]["esimList"]) == 17
    assert client.asked == [1]


def test_a_full_page_is_followed_by_the_next() -> None:
    size = EsimAccessClient.PROFILE_PAGE_SIZE
    client = Recorder([_profiles(size), _profiles(size, size), _profiles(7, size * 2)])

    result = client.query_profiles(order_no="")

    assert len(result["obj"]["esimList"]) == size * 2 + 7
    assert client.asked == [1, 2, 3]


def test_every_profile_is_kept_exactly_once() -> None:
    size = EsimAccessClient.PROFILE_PAGE_SIZE
    client = Recorder([_profiles(size), _profiles(3, size)])

    names = [p["esimTranNo"] for p in client.query_profiles(order_no="")["obj"]["esimList"]]

    assert len(names) == len(set(names)) == size + 3


def test_an_exactly_full_last_page_stops_at_the_empty_one() -> None:
    """Oxirgi sahifa to'la bo'lsa, keyingisi bo'sh qaytadi va halqa shunda
    to'xtaydi -- aks holda cheksiz so'rov ketardi."""

    size = EsimAccessClient.PROFILE_PAGE_SIZE
    client = Recorder([_profiles(size)])

    result = client.query_profiles(order_no="")

    assert len(result["obj"]["esimList"]) == size
    assert client.asked == [1, 2]


def test_it_cannot_loop_forever() -> None:
    """Ta'minotchi har safar to'la sahifa qaytarsa ham halqa chegaralangan."""

    size = EsimAccessClient.PROFILE_PAGE_SIZE
    client = Recorder([_profiles(size)] * 1000)

    client.query_profiles(order_no="")

    assert len(client.asked) == EsimAccessClient.PROFILE_PAGE_LIMIT


def test_the_rest_of_the_answer_survives() -> None:
    """Chaqiruvchi kod javobning boshqa maydonlarini ham o'qiydi."""

    client = Recorder([_profiles(2)])

    result = client.query_profiles(order_no="")

    assert result["success"] is True
    assert result["obj"]["note"] == "keep"
