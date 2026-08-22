# ADR-0001 — Which address is the visitor, behind Cloudflare

**Status:** accepted, 2026-08-22
**Applies to:** `QulaySIM-backend` and `QulaySIM-admin` (both must change together)

## Context

Three things decide behaviour from a visitor's address:

| Consumer | Where | What it does with it |
|---|---|---|
| Rate limits | `app/core/ratelimit.py` | per-address buckets for login, promo, catalogue |
| ATMOS callback | `app/services/atmos.py` | refuses a callback from outside ATMOS's published range |
| Admin lockout | `QulaySIM-admin`, django-axes | locks (username, ip_address) after six wrong passwords |

On 2026-08-20 the domain moved behind Cloudflare and all three broke at once,
because the chain gained a hop:

```
visitor → Cloudflare edge → Caddy → (FastAPI | Django)
```

Measured on this deployment: **Caddy replaces `X-Forwarded-For` with the address
it received the connection from, rather than appending to it.** So after
Cloudflare, `X-Forwarded-For` holds a Cloudflare edge and nothing in the chain
carries the visitor — except `CF-Connecting-IP`, which Cloudflare writes and does
not let a caller override.

The cost of getting it wrong, in the order we found out:

1. A customer paid 79 999 so'm. The ATMOS callback arrived, the source-range
   check saw `162.158.172.93` (Cloudflare), refused it, and the order stayed
   pending with no eSIM. The only trace was one log line.
2. Every rate limit collapsed onto one bucket per Cloudflare edge.
3. Admin lockout counted failed logins against a rotating edge, so six wrong
   passwords from one attacker never added up to six anywhere. Found by an
   audit, not by a test.

## Decision

**Read `CF-Connecting-IP`, but only when Cloudflare actually delivered the
request.** A header is worth exactly as much as the hop that wrote it: anyone
reaching the origin directly can send `CF-Connecting-IP: 8.8.8.8`, and being
believed would hand them the callback's range check, somebody else's rate-limit
bucket, and the lockout counter.

"Actually delivered" means: the peer — the right-most `X-Forwarded-For` entry,
falling back to the socket — is inside Cloudflare's published ranges.

One switch controls both services: **`TRUST_CLOUDFLARE_CLIENT_IP`** in `.env`.
Off, both fall back to `X-Forwarded-For`, read right-most so a caller cannot
seed its own.

### Where it lives

| | Backend | Admin |
|---|---|---|
| Ranges | `app/core/cloudflare_ips.py` | `config/client_address.py` |
| Rule | `client_ip()` in `app/core/ratelimit.py` | `TrustedClientIpMiddleware` |
| Tests | `tests/unit/test_ratelimit.py` | `config/tests_client_address.py` |

## Why it is written twice

Two repositories, two runtimes, two languages: there is no module to share. What
is shared is the switch and this document. **Change one, change both**, and
refresh the Cloudflare ranges in both files together — they are fetched from
`https://api.cloudflare.com/client/v4/ips` and change a handful of times a
decade.

## Rejected: enforce it in the firewall

The obvious alternative is to narrow ports 80 and 443 to Cloudflare's ranges,
so a direct request cannot arrive at all and the header needs no verification.

Rejected because **this host also serves a second project reached directly by
IP** (`dental.…sslip.io`). A host-wide rule would take that project offline, and
a per-site rule means editing a Caddyfile shared with it. The application-level
check gives the same property, is testable, and touches nothing outside these
two repos.

Worth revisiting if the second project moves off this host or behind Cloudflare
itself.

## Consequences

- Both services now need `TRUST_CLOUDFLARE_CLIENT_IP=true` in production, and
  it must be passed through `docker-compose.yml`, which enumerates variables
  rather than forwarding the file. A setting added to the application but not to
  the compose file is invisible — that happened once already.
- If Cloudflare is ever removed, set the switch to `false` in one place.
- New Cloudflare ranges stop being recognised until both lists are refreshed,
  and the failure is safe: the header is ignored, and the fallback applies.
