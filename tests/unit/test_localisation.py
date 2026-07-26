from __future__ import annotations

from dataclasses import dataclass

from app.domain.localisation import localise, normalise_language


@dataclass
class Row:
    title: str = "English"
    title_ru: str = ""
    title_uz: str = ""


class TestNormaliseLanguage:
    def test_known(self) -> None:
        assert normalise_language("uz") == "uz"

    def test_regional_variant_stripped(self) -> None:
        assert normalise_language("ru-RU") == "ru"

    def test_unknown_falls_back(self) -> None:
        assert normalise_language("de") == "en"

    def test_none_falls_back(self) -> None:
        assert normalise_language(None) == "en"


class TestLocalise:
    def test_returns_translation(self) -> None:
        assert localise(Row(title_uz="Oʻzbekcha"), "title", "uz") == "Oʻzbekcha"

    def test_empty_translation_falls_back_to_english(self) -> None:
        """An untranslated CMS row must render English, never an empty block."""
        assert localise(Row(title_uz=""), "title", "uz") == "English"

    def test_english_requested_ignores_translations(self) -> None:
        assert localise(Row(title_ru="Русский"), "title", "en") == "English"
