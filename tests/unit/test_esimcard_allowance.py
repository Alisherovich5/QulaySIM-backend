"""What an eSIMCard profile is worth once it is in our database.

The wholesaler's listing carries an id, an iccid, a status, the QR and a
free-text bundle name — no megabytes and no expiry. Everything sold through it
therefore read 0 GB on the customer's page, six profiles including one given
away from the admin, because the allowance was never written from the only
place that knows it: the plan.
"""

from __future__ import annotations

from typing import ClassVar

from app.integrations import esimcard_sync


class _Plan:
    id = 7
    data_amount_mb = 20480
    validity_days = 30
    provider_package_code = "pkg"
    offers: ClassVar[list] = []


class _Item:
    id = 1
    plan = _Plan()


class _Row:
    line_key = "1:1"
    package_code = "pkg"
    supplier_ref = "ref-1"
    iccid = ""
    state = "done"


class _Order:
    id = 99
    customer_id = 5
    provider = "esimcard"
    provider_status = ""
    items: ClassVar[list] = [_Item()]


class _Remote:
    def __init__(self, status: str) -> None:
        self.supplier_id = "ref-1"
        self.iccid = "8910"
        self.status = status
        self.universal_link = (
            "https://esimsetup.apple.com/esim_qrcode_provisioning?carddata=LPA:1$smdp$code"
        )
        self.bundle = "20GB"


class _Client:
    is_configured = True

    def __init__(self, status: str) -> None:
        self._status = status

    def find_esims(self, refs, **_):
        return {"ref-1": _Remote(self._status)}


class _Query:
    def filter(self, *a, **k):
        return self

    def first(self):
        return None


class _Session:
    def __init__(self) -> None:
        self.added: list = []
        self.committed = False

    def query(self, *a, **k):
        return _Query()

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True


def _written(monkeypatch, status: str) -> dict:
    captured: dict = {}

    class _ESIM:
        # The real class is also used in the `db.query(...).filter(...)` above,
        # so the stub has to carry the two columns that expression names.
        provider = "esimcard"
        provider_esim_tran_no = ""

        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(esimcard_sync, "ESIM", _ESIM)
    monkeypatch.setattr(esimcard_sync, "render_qr_data_url", lambda payload: "data:,")
    monkeypatch.setattr(esimcard_sync.ledger, "purchases_for", lambda db, oid, provider: [_Row()])
    monkeypatch.setattr(esimcard_sync.ledger, "DONE", "done", raising=False)
    monkeypatch.setattr(esimcard_sync.ledger, "CLAIMED", "claimed", raising=False)

    touched = esimcard_sync.sync_order_profiles(_Session(), _Order(), _Client(status))
    assert touched == 1, "profile was not written at all"
    return captured


def test_hajm_tarifdan_yoziladi(monkeypatch):
    # Yetkazib beruvchi megabaytni umuman yubormaydi — tarif yagona manba.
    assert _written(monkeypatch, "Released")["data_total_mb"] == 20480


def test_ornatilmagan_profil_muddatsiz_qoladi(monkeypatch):
    # "Released" — sotib olingan, lekin hali o'rnatilmagan. Sanoq boshlanmagan.
    assert _written(monkeypatch, "Released").get("expires_at") is None


def test_ornatilgan_profilga_muddat_yoziladi(monkeypatch):
    from datetime import timedelta

    from app.db.base import utcnow

    values = _written(monkeypatch, "Installed")
    assert values["status"] == "active"
    left = values["expires_at"] - utcnow()
    assert timedelta(days=29) < left <= timedelta(days=30), left
