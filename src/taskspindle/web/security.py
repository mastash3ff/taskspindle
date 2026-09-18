"""Loopback, origin, CSRF and body-size guards shared by the dashboard's routes.

Every check here is defense in depth around a single fact: the dashboard binds to loopback.
``SecurityMiddleware`` gates every route, read or write, on the HTTP ``Host`` header alone,
which blocks DNS rebinding while still allowing a deliberate ``--host 0.0.0.0`` bind. An operator
running behind a private VPN may name exact remote ``Host`` netlocs in ``[web] allowed_hosts``
(see :mod:`.remote`); that only widens which ``Host`` values this check accepts. The mutating
surfaces - the dispatch policy and the optional Codex AI-policy adapter - additionally require a
request that resolves to the local machine at the TCP layer too, comes from the page the
dashboard itself served, and carries the per-process CSRF token that page received, before its
body is even read - unless ``[web] allow_remote_policy`` is also set and the request's ``Host``
is one of those same configured remote netlocs.
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
from typing import Any
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

__all__ = [
    "SecurityMiddleware",
    "csrf_valid",
    "loopback_address",
    "loopback_host",
    "no_store",
    "parse_netloc",
    "read_json_object",
    "remote_allowed_host",
    "remote_policy_allowed",
    "same_origin",
    "trusted_loopback",
]


def loopback_address(value: str) -> bool:
    """Return whether ``value`` is a numeric loopback address."""
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def loopback_host(value: str | None) -> bool:
    """Accept an exact localhost name or numeric loopback Host, with an optional port."""
    if not value or any(character in value for character in "/?#@"):
        return False
    try:
        parsed = urlsplit(f"//{value}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if port == 0:
        return False
    return hostname == "localhost" or (hostname is not None and loopback_address(hostname))


def trusted_loopback(request: Request) -> bool:
    """Require both the TCP peer and HTTP Host to resolve to the local machine."""
    peer = request.client.host if request.client is not None else ""
    return loopback_address(peer) and loopback_host(request.headers.get("host"))


_HOSTNAME_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME_RE = re.compile(rf"^{_HOSTNAME_LABEL}(\.{_HOSTNAME_LABEL})*$")


def _valid_host_token(hostname: str) -> bool:
    """A numeric IPv4/IPv6 literal or a syntactically valid DNS hostname; nothing else."""
    try:
        ipaddress.ip_address(hostname.split("%", 1)[0])
        return True
    except ValueError:
        pass
    return bool(_HOSTNAME_RE.fullmatch(hostname))


def _has_forbidden_netloc_char(value: str) -> bool:
    """Reject path/query/fragment/userinfo delimiters, and any raw whitespace or C0 control."""
    return any(
        character in "/?#@" or ord(character) < 0x20 or character == "\x7f" or character.isspace()
        for character in value
    )


def parse_netloc(value: str) -> tuple[str, int | None] | None:
    """Parse ``host[:port]`` strictly: no path, query, fragment, userinfo, control, or whitespace."""
    if not value or _has_forbidden_netloc_char(value):
        return None
    try:
        parsed = urlsplit(f"//{value}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if hostname is None or port == 0 or not _valid_host_token(hostname):
        return None
    return hostname, port


def remote_allowed_host(value: str | None, allowed_hosts: frozenset[str]) -> bool:
    """True when ``value`` (the request's ``Host``) exactly names a configured remote netloc.

    Comparison is by parsed ``(hostname, port)``, not raw string equality, so ``allowed_hosts``
    entries and incoming ``Host`` headers agree on case and an implicit default port; nothing here
    accepts a prefix, suffix, or wildcard match.
    """
    if not value:
        return False
    parsed = parse_netloc(value)
    if parsed is None:
        return False
    return any(parse_netloc(candidate) == parsed for candidate in allowed_hosts)


def remote_policy_allowed(
    request: Request, *, allow_remote_policy: bool, allowed_hosts: frozenset[str]
) -> bool:
    """Replace the loopback-peer requirement only for an explicit opt-in and an explicit Host.

    A caller-supplied ``Forwarded``/``X-Forwarded-*`` header is never consulted here; only the
    ``Host`` this connection actually negotiated counts, so a request cannot claim to be a
    configured remote host it did not actually arrive as.
    """
    if trusted_loopback(request):
        return True
    return allow_remote_policy and remote_allowed_host(request.headers.get("host"), allowed_hosts)


class SecurityMiddleware(BaseHTTPMiddleware):
    """Gate every request on ``Host`` and attach defense-in-depth headers to every response.

    The dashboard's actual trust boundary is the ``0600`` task database extended to a local
    port (see ``docs/dashboard.md``), not the TCP peer: an operator who runs
    ``taskspindle web --host 0.0.0.0`` has deliberately widened that boundary to the LAN and
    already sees a stderr warning for it (``cli.py``). What must never work is a browser on
    that operator's machine being tricked, via DNS rebinding, into sending a same-origin
    request whose ``Host`` header still names this port while the TCP connection lands on a
    remote attacker's server pretending to be ``127.0.0.1``. Checking only ``Host`` here -
    every route, read or write - closes that hole without breaking a deliberate non-loopback
    bind. ``allowed_hosts`` extends this same Host allowlist with exact, operator-configured
    remote netlocs (see ``docs/dashboard.md``); it is empty unless ``[web] allowed_hosts`` names
    them. The five mutating routes layer ``remote_policy_allowed`` (peer-and-Host loopback, or an
    explicit remote opt-in) on top of this for a second, stricter reason: their state changes
    should not depend on trusting the network path at all when the operator hasn't chosen to
    widen it.
    """

    def __init__(self, app: Any, allowed_hosts: frozenset[str] = frozenset()) -> None:
        super().__init__(app)
        self._allowed_hosts = allowed_hosts

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        host = request.headers.get("host")
        if not (loopback_host(host) or remote_allowed_host(host, self._allowed_hosts)):
            response: Response = no_store({"error": "LOOPBACK_REQUIRED"}, status=403)
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


def same_origin(request: Request) -> bool:
    """Require an ``Origin`` header that names exactly this scheme and Host."""
    host = request.headers.get("host")
    if not host:
        return False
    expected = f"{request.url.scheme}://{host}"
    return request.headers.get("origin") == expected


def csrf_valid(request: Request, token: str) -> bool:
    """Constant-time comparison of the per-process CSRF token against the request header."""
    supplied = request.headers.get("x-taskspindle-csrf", "")
    return secrets.compare_digest(supplied, token)


def no_store(payload: Any, status: int = 200) -> JSONResponse:
    """A JSON response browsers and intermediaries must never cache."""
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


async def read_json_object(
    request: Request, max_bytes: int
) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Read and parse a bounded JSON object body, or the error response to return instead."""
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        return None, no_store({"error": "JSON_REQUIRED"}, status=415)
    body_bytes = bytearray()
    async for chunk in request.stream():
        if len(body_bytes) + len(chunk) > max_bytes:
            return None, no_store({"error": "REQUEST_TOO_LARGE"}, status=413)
        body_bytes.extend(chunk)
    try:
        body = json.loads(body_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, no_store({"error": "INVALID_JSON"}, status=400)
    if not isinstance(body, dict):
        return None, no_store({"error": "INVALID_JSON"}, status=400)
    return body, None
