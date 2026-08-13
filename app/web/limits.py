"""Capacity and abuse protection for public instances.

The check queue physically fits ~60 site visits per hour (CHECK_SPACING_SECONDS
apart, WAF-mandated). Everything here exists to keep a public instance inside
that budget: per-IP rate limits, an on-demand result cache, and an adaptive
monitor interval that stretches as the instance fills up.
"""

import ipaddress
import time

from ..config import settings
from ..core.models import CheckResult


def adaptive_interval(active_monitors: int, base_interval: int, spacing: int) -> int:
    """Stretch the re-check interval so scheduled checks use at most ~half of
    the queue's capacity, leaving room for on-demand checks and captcha
    retries: interval >= 2 * N * spacing."""
    return max(base_interval, 2 * active_monitors * spacing)


class RateLimiter:
    """In-memory sliding-window limiter, keyed by (bucket, client)."""

    def __init__(self):
        self._hits: dict[tuple[str, str], list[float]] = {}

    def allow(self, bucket: str, client: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        key = (bucket, client)
        hits = [t for t in self._hits.get(key, []) if now - t < window_seconds]
        if len(hits) >= limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        if len(self._hits) > 10_000:  # drop stale keys, don't grow unbounded
            self._hits = {k: v for k, v in self._hits.items()
                          if v and now - v[-1] < window_seconds}
        return True


class ResultCache:
    """Short-lived cache of on-demand results so repeat lookups of the same
    expediente don't burn queue slots."""

    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self._items: dict[tuple, tuple[float, CheckResult]] = {}

    @staticmethod
    def _key(expediente_id: str, fecha: str, anio: str) -> tuple:
        return (expediente_id, fecha, anio)

    def get(self, expediente_id: str, fecha: str, anio: str) -> CheckResult | None:
        item = self._items.get(self._key(expediente_id, fecha, anio))
        if item is None:
            return None
        stored_at, result = item
        if time.monotonic() - stored_at > self.ttl:
            return None
        return result

    def put(self, expediente_id: str, fecha: str, anio: str, result: CheckResult) -> None:
        self._items[self._key(expediente_id, fecha, anio)] = (time.monotonic(), result)
        if len(self._items) > 1000:
            now = time.monotonic()
            self._items = {k: v for k, v in self._items.items() if now - v[0] <= self.ttl}


rate_limiter = RateLimiter()
result_cache = ResultCache(settings.ondemand_cache_ttl)


def client_ip(request) -> str:
    """Real client address for rate limiting and Turnstile exemption. Proxy
    headers are honoured only when TRUST_PROXY is set (behind a reverse proxy
    we control).

    Prefer CF-Connecting-IP: behind Cloudflare (tunnel or proxied DNS) the
    edge sets it to the true visitor IP and a client cannot forge it. Plain
    X-Forwarded-For is client-appendable, so trusting its first entry would
    let a visitor spoof an exempt LAN IP to bypass Turnstile and rate limits."""
    if settings.trust_proxy:
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _exempt_networks() -> list:
    nets = []
    for cidr in settings.turnstile_exempt_cidrs.split(","):
        cidr = cidr.strip()
        if not cidr:
            continue
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            pass
    return nets


_EXEMPT_NETS = _exempt_networks()


def turnstile_exempt(ip: str) -> bool:
    """True if a request from this source IP should skip the Turnstile check
    (trusted LAN / VPN ranges)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _EXEMPT_NETS)


async def verify_turnstile(token: str | None, ip: str) -> bool:
    """Validate a Cloudflare Turnstile token. Always true when not configured
    or when the source IP is in an exempt network."""
    if not settings.turnstile_secret_key:
        return True
    if turnstile_exempt(ip):
        return True
    if not token:
        return False
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": settings.turnstile_secret_key,
                      "response": token, "remoteip": ip},
            )
        return bool(resp.json().get("success"))
    except Exception:
        return False
