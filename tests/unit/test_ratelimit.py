from __future__ import annotations

import pytest

from app.core.ratelimit import parse_rule


@pytest.mark.parametrize(
    ("rule", "expected"),
    [("10/300", (10, 300)), ("1/60", (1, 60)), ("120/60", (120, 60))],
)
def test_parse_rule(rule: str, expected: tuple[int, int]) -> None:
    assert parse_rule(rule) == expected


class _Request:
    """The two attributes client_ip reads, and nothing else."""

    def __init__(self, forwarded: str | None, peer: str | None = "10.0.0.1") -> None:
        self.headers = {"x-forwarded-for": forwarded} if forwarded is not None else {}
        self.client = type("Peer", (), {"host": peer})() if peer else None


@pytest.mark.parametrize(
    ("forwarded", "expected", "why"),
    [
        # nginx APPENDS the peer, so the real address is on the right. Reading
        # the left-hand value let a caller mint a new bucket per request just by
        # rotating the header — the limit was decorative.
        ("203.0.113.9, 77.37.54.14", "77.37.54.14", "spoofed left-hand entry ignored"),
        ("77.37.54.14", "77.37.54.14", "single hop is the peer"),
        ("evil<script>alert(1)</script>, 77.37.54.14", "77.37.54.14", "injection ignored"),
        ("  203.0.113.9 ,  77.37.54.14  ", "77.37.54.14", "whitespace tolerated"),
        ("2001:db8::1", "2001:db8::1", "IPv6 accepted"),
        # Anything that is not an address falls back to the socket peer rather
        # than becoming a Redis key or a log field.
        ("not-an-ip", "10.0.0.1", "unparseable falls back to the peer"),
        ("", "10.0.0.1", "empty header falls back to the peer"),
        (None, "10.0.0.1", "no header at all"),
    ],
)
def test_client_ip_cannot_be_chosen_by_the_caller(
    forwarded: str | None, expected: str, why: str
) -> None:
    from app.core.ratelimit import client_ip

    assert client_ip(_Request(forwarded)) == expected, why


def test_client_ip_without_a_peer() -> None:
    from app.core.ratelimit import client_ip

    assert client_ip(_Request(None, peer=None)) == "unknown"
