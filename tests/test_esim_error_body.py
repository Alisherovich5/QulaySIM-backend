import pytest

from app.integrations.esim_access import EsimAccessClient, EsimAccessError


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _run(monkeypatch, payload):
    # CI da ESIMACCESS_ACCESS_CODE yo'q, shuning uchun klient tarmoqqa
    # chiqmasdan "not configured" deb tashlaydi va test o'lchamoqchi bo'lgan
    # joyga umuman yetib bormaydi. Sozlangan holat shu yerda beriladi.
    from app.core.config import settings

    monkeypatch.setattr(settings, "esimaccess_access_code", "test-code", raising=False)
    monkeypatch.setattr(settings, "esimaccess_secret_key", "test-secret", raising=False)
    c = EsimAccessClient()
    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp(payload))
    with pytest.raises(EsimAccessError) as e:
        c._post("/x")
    return str(e.value)


def test_xabarsiz_kod_tanani_olib_keladi(monkeypatch):
    msg = _run(monkeypatch, {"success": False, "errorCode": "200007", "obj": {"iccid": "893"}})
    assert "200007" in msg
    assert "893" in msg, msg  # tana xabarga tushdi
    assert "Unknown" not in msg


def test_xabar_bor_bolsa_osha_ishlatiladi(monkeypatch):
    msg = _run(
        monkeypatch,
        {"success": False, "errorCode": "1", "errorMessage": "iccid noto'g'ri", "obj": 1},
    )
    assert "iccid noto'g'ri" in msg
    assert "body=" not in msg
