from __future__ import annotations

import pytest

from app.core.ratelimit import parse_rule


@pytest.mark.parametrize(
    ("rule", "expected"),
    [("10/300", (10, 300)), ("1/60", (1, 60)), ("120/60", (120, 60))],
)
def test_parse_rule(rule: str, expected: tuple[int, int]) -> None:
    assert parse_rule(rule) == expected
