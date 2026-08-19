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


class _CfRequest:
    """A request as it arrives with Cloudflare in front of Caddy."""

    def __init__(
        self, forwarded: str | None = None, cf: str | None = None, peer: str = "10.0.0.1"
    ) -> None:
        self.headers: dict[str, str] = {}
        if forwarded is not None:
            self.headers["x-forwarded-for"] = forwarded
        if cf is not None:
            self.headers["cf-connecting-ip"] = cf
        self.client = type("Peer", (), {"host": peer})()


@pytest.mark.parametrize(
    ("forwarded", "expected", "why"),
    [
        # Cloudflare states the visitor, Caddy appends the edge it heard from.
        (
            "203.0.113.9, 172.70.100.5",
            "203.0.113.9",
            "two hops: the visitor is second from the right",
        ),
        # …and a caller who forges a prefix still cannot choose the bucket,
        # because the count is made from the right.
        ("1.1.1.1, 203.0.113.9, 172.70.100.5", "203.0.113.9", "forged prefix ignored"),
        ("203.0.113.9", "203.0.113.9", "a short header does not underflow"),
    ],
)
def test_client_ip_with_cloudflare_in_front(
    forwarded: str, expected: str, why: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this guards: off by one and every visitor in the country shares
    one rate-limit bucket, so real customers start seeing 429s about somebody
    else's traffic."""
    from app.core import ratelimit

    monkeypatch.setattr(ratelimit.settings, "trusted_proxy_hops", 2)
    assert ratelimit.client_ip(_CfRequest(forwarded)) == expected, why


def test_cf_connecting_ip_is_ignored_until_the_origin_is_locked_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The security half. While the origin still answers the open internet,
    anybody can set this header directly and pick whose bucket to spend."""
    from app.core import ratelimit

    monkeypatch.setattr(ratelimit.settings, "trust_cloudflare_client_ip", False)
    request = _CfRequest(forwarded="203.0.113.9", cf="9.9.9.9")
    assert ratelimit.client_ip(request) == "203.0.113.9"


def test_cf_connecting_ip_is_believed_once_it_can_be(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import ratelimit

    monkeypatch.setattr(ratelimit.settings, "trust_cloudflare_client_ip", True)
    assert ratelimit.client_ip(_CfRequest(forwarded="1.2.3.4", cf="203.0.113.9")) == "203.0.113.9"


def test_a_junk_cf_header_falls_back_rather_than_becoming_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import ratelimit

    monkeypatch.setattr(ratelimit.settings, "trust_cloudflare_client_ip", True)
    monkeypatch.setattr(ratelimit.settings, "trusted_proxy_hops", 1)
    request = _CfRequest(forwarded="203.0.113.9", cf="'; DROP TABLE orders; --")
    assert ratelimit.client_ip(request) == "203.0.113.9"
