"""Landing-content language selection.

Django stores English as the base with optional _ru/_uz columns. An empty
translation must fall back to English rather than render a blank section.
"""

from __future__ import annotations

from typing import Any

SUPPORTED_LANGUAGES = ("en", "ru", "uz")
DEFAULT_LANGUAGE = "en"


def normalise_language(raw: str | None) -> str:
    if not raw:
        return DEFAULT_LANGUAGE
    code = raw.split("-", 1)[0].strip().lower()
    return code if code in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def localise(obj: Any, field: str, language: str) -> str:
    """Return `field_<lang>` when non-empty, otherwise the English base."""
    base = getattr(obj, field, "") or ""
    if language == DEFAULT_LANGUAGE:
        return str(base)
    translated = getattr(obj, f"{field}_{language}", "") or ""
    return str(translated or base)
