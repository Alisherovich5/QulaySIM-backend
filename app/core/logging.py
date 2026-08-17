"""Structured JSON logging with request correlation."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

import re

import structlog

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def _add_request_id(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    event_dict["request_id"] = request_id_var.get()
    return event_dict


#: Field names whose value is a secret whatever it looks like.
_SECRET_FIELDS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "password",
        "api_key",
        "secret",
        "signature",
        "authorization",
        "cookie",
        "card",
        "pan",
        "activation_code",
        "qr",
    }
)

#: Values that betray a person or a credential even under an innocent field name.
_PATTERNS = (
    # Email — kept recognisable as an email without naming anyone.
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    # Uzbek mobile number, with or without punctuation.
    re.compile(r"\+?998[\s()-]?\d{2}[\s()-]?\d{3}[\s()-]?\d{2}[\s()-]?\d{2}"),
    # Card-shaped run of digits.
    re.compile(r"\b\d{13,19}\b"),
    # JWT.
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"),
)


def _mask_text(value: str) -> str:
    for pattern in _PATTERNS:
        value = pattern.sub("[maskalangan]", value)
    return value


def _mask(_logger: object, _name: str, event_dict: dict) -> dict:
    """Keep credentials and personal data out of the log file.

    Logs are read by more people than the database is, they are copied into chat
    messages when something breaks, and they outlive the incident. A token in a
    log line is a token that has leaked; a phone number in one is a customer we
    exposed for no operational gain.

    Field names are checked first because a secret does not always look like
    one, then the values are scanned — a message string built with an email in it
    is the common accident, and it would pass any name-based check.
    """
    for key, value in list(event_dict.items()):
        if key.lower() in _SECRET_FIELDS:
            event_dict[key] = "[maskalangan]"
        elif isinstance(value, str) and len(value) > 6:
            event_dict[key] = _mask_text(value)
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_request_id,
            _mask,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
