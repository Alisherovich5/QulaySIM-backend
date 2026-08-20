"""Whose word to take about a visitor's address.

`CF-Connecting-IP` is the only header that reliably carries the real client once
Cloudflare is in front: Cloudflare writes it, refuses to let a caller override
it, and Caddy passes it through untouched. Everything else in the chain lies by
omission — measured on this deployment, Caddy replaces X-Forwarded-For with the
address it heard from, which is a Cloudflare edge.

But a header is only as good as the hop that wrote it. Anyone who reaches the
origin directly can send `CF-Connecting-IP: 8.8.8.8` and be believed — which
would hand them the ATMOS callback's source-range check, somebody else's
rate-limit bucket, and the admin's lockout counter.

So the header is believed only when the request actually arrived through
Cloudflare, and that is checked against Cloudflare's own published ranges. The
firewall would be the other place to enforce this, and it is not available here:
this host also serves a second project that is not behind Cloudflare, so
narrowing ports 80 and 443 to Cloudflare would take that project offline.

The list below is https://api.cloudflare.com/client/v4/ips, fetched
2026-08-20 (etag 38f79d050aa027e3be3865e495dcc9bc). It changes rarely — a handful of times a
decade — and `test_cloudflare_ranges_are_current` is the reminder to refresh it.
"""

from __future__ import annotations

from ipaddress import ip_address, ip_network

#: https://api.cloudflare.com/client/v4/ips — ipv4_cidrs
CLOUDFLARE_IPV4 = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
)

#: https://api.cloudflare.com/client/v4/ips — ipv6_cidrs
CLOUDFLARE_IPV6 = (
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

_NETWORKS = tuple(ip_network(cidr) for cidr in CLOUDFLARE_IPV4 + CLOUDFLARE_IPV6)


def is_cloudflare(address: str) -> bool:
    """Whether an address belongs to Cloudflare's edge."""
    try:
        parsed = ip_address(address.strip())
    except ValueError:
        return False
    return any(parsed in network for network in _NETWORKS)
