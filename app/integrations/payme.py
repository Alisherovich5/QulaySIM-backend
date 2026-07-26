"""Payme (Paycom) Merchant API — the JSON-RPC protocol layer.

Payme calls us; we never call Payme except to build a checkout URL. It may
retry any method at any time, so every handler here is replayable: calling
CreateTransaction twice with the same id returns the same transaction rather
than creating a second one, and performing an already-performed transaction
returns the original timestamps.

Protocol facts that shape this code:
  * amounts are integers in tiyin (1 UZS = 100 tiyin);
  * timestamps are milliseconds since the epoch, and Payme compares the values
    it gets back against what it sent, so they are stored verbatim;
  * error messages must be objects keyed by language, not strings;
  * a transaction left unconfirmed for 12 hours must be cancelled with
    reason 4 rather than performed.
"""

from __future__ import annotations

import base64
import hmac
import time
from typing import Any

# --- JSON-RPC / Payme error codes -------------------------------------------
INVALID_AMOUNT = -31001
TRANSACTION_NOT_FOUND = -31003
CANNOT_PERFORM = -31008
# The -31050..-31099 block is reserved for merchant-defined account errors.
ACCOUNT_NOT_FOUND = -31050
ACCOUNT_NOT_PAYABLE = -31051
METHOD_NOT_ALLOWED = -32400
INSUFFICIENT_PRIVILEGES = -32504
METHOD_NOT_FOUND = -32601

TRANSACTION_TIMEOUT_MS = 12 * 60 * 60 * 1000
TIMEOUT_CANCEL_REASON = 4

# State machine, Payme's numbering.
STATE_CREATED = 1
STATE_PERFORMED = 2
STATE_CANCELLED = -1
STATE_CANCELLED_AFTER_PERFORM = -2

MESSAGES: dict[int, dict[str, str]] = {
    INVALID_AMOUNT: {
        "ru": "Неверная сумма",
        "uz": "Summa noto'g'ri",
        "en": "Invalid amount",
    },
    TRANSACTION_NOT_FOUND: {
        "ru": "Транзакция не найдена",
        "uz": "Tranzaksiya topilmadi",
        "en": "Transaction not found",
    },
    CANNOT_PERFORM: {
        "ru": "Невозможно выполнить операцию",
        "uz": "Amalni bajarib bo'lmadi",
        "en": "Unable to perform operation",
    },
    ACCOUNT_NOT_FOUND: {
        "ru": "Заказ не найден",
        "uz": "Buyurtma topilmadi",
        "en": "Order not found",
    },
    ACCOUNT_NOT_PAYABLE: {
        "ru": "Заказ не доступен для оплаты",
        "uz": "Buyurtma to'lov uchun mavjud emas",
        "en": "Order is not available for payment",
    },
    METHOD_NOT_ALLOWED: {
        "ru": "Метод не разрешён",
        "uz": "Metod ruxsat etilmagan",
        "en": "Method not allowed",
    },
    INSUFFICIENT_PRIVILEGES: {
        "ru": "Недостаточно привилегий",
        "uz": "Ruxsat yetarli emas",
        "en": "Insufficient privileges",
    },
    METHOD_NOT_FOUND: {
        "ru": "Метод не найден",
        "uz": "Metod topilmadi",
        "en": "Method not found",
    },
}


def now_ms() -> int:
    return int(time.time() * 1000)


def error(request_id: Any, code: int, data: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": code,
        "message": MESSAGES.get(code, {"ru": "Ошибка", "uz": "Xatolik", "en": "Error"}),
    }
    if data is not None:
        body["data"] = data
    return {"id": request_id, "error": body}


def result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"id": request_id, "result": payload}


def authorised(header: str | None, keys: tuple[str, ...]) -> bool:
    """Verify the Basic header Payme sends: base64("Paycom:<key>").

    Compared with compare_digest so a wrong key cannot be recovered by timing.
    Both the live and sandbox keys are accepted, which is what lets the same
    deployment pass Payme's test suite.
    """
    try:
        scheme, _, encoded = (header or "").partition(" ")
        if scheme.lower() != "basic":
            return False
        _login, _, key = base64.b64decode(encoded).decode().partition(":")
    except Exception:  # noqa: BLE001 — any malformed header is simply unauthorised
        return False
    if not key:
        return False
    return any(hmac.compare_digest(key, candidate) for candidate in keys if candidate)


def checkout_url(
    *,
    base_url: str,
    merchant_id: str,
    account_field: str,
    account_value: str,
    amount_tiyin: int,
    return_url: str = "",
) -> str:
    """Build the hosted checkout link the customer is sent to.

    Payme takes its parameters as a base64-encoded, semicolon-separated string
    in the path — there is no POST form.
    """
    parts = [
        f"m={merchant_id}",
        f"ac.{account_field}={account_value}",
        f"a={amount_tiyin}",
    ]
    if return_url:
        parts.append(f"c={return_url}")
    encoded = base64.b64encode(";".join(parts).encode()).decode()
    return f"{base_url.rstrip('/')}/{encoded}"
