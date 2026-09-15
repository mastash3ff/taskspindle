"""Loopback, origin, CSRF and body-size guards shared by the dashboard's routes.

Every check here is defense in depth around a single fact: the dashboard binds to loopback.
``SecurityMiddleware`` gates every route, read or write, on the HTTP ``Host`` header alone,
which blocks DNS rebinding while still allowing a deliberate ``--host 0.0.0.0`` bind. The
mutating surfaces - the dispatch policy and the optional Codex AI-policy adapter - additionally
require a request that resolves to the local machine at the TCP layer too, comes from the page
the dashboard itself served, and carries the per-process CSRF token that page received, before
its body is even read.
"""

from __future__ import annotations

import ipaddress
import json
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
    "read_json_object",
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
    bind. The five mutating routes layer ``trusted_loopback`` (peer *and* Host) on top of this
    for a second, stricter reason: their state changes should not depend on trusting the
    network path at all when the operator hasn't chosen to widen it.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not loopback_host(request.headers.get("host")):
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
