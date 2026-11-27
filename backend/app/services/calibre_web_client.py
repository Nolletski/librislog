"""Authenticated HTTP client for Calibre-Web NextGen.

Credentials and the reusable Flask session are kept server-side. This module
never exposes cookies, CSRF tokens, or passwords in API response payloads.
"""

from __future__ import annotations

import ipaddress
import logging
import mimetypes
import socket
from datetime import datetime, timezone
from typing import BinaryIO
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx
from cryptography.fernet import InvalidToken
from httpcore._backends.auto import AutoBackend
from sqlmodel import Session

from app.auth import decrypt_api_key, encrypt_api_key
from app.models import CalibreWebConnection

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30.0


def _resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve a hostname once and return only validated public addresses."""
    try:
        resolved = tuple(dict.fromkeys(
            str(ipaddress.ip_address(item[4][0]))
            for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ))
    except (OSError, ValueError) as exc:
        raise ValueError("Calibre-Web hostname could not be resolved safely.") from exc
    if not resolved or any(not ipaddress.ip_address(address).is_global for address in resolved):
        raise ValueError("Calibre-Web hostname must resolve only to public IP addresses.")
    return resolved


class _PinnedDNSBackend(httpcore.AsyncNetworkBackend):
    """Connect only to the public IPs validated for the configured CWNG host.

    httpcore still receives the original hostname as the TLS server_hostname,
    so certificate validation and SNI remain bound to the configured hostname.
    The DNS lookup used by the socket connection is replaced with this fixed
    address set, closing the validation/connection DNS-rebinding window.
    """

    def __init__(self, hostname: str, port: int, addresses: tuple[str, ...]):
        self.hostname = hostname.rstrip(".").lower()
        self.port = port
        self.addresses = addresses
        self.backend = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        if host.rstrip(".").lower() != self.hostname or port != self.port:
            raise OSError("Calibre-Web transport refused an unexpected destination.")
        last_error: Exception | None = None
        for address in self.addresses:
            try:
                return await self.backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise OSError("No validated Calibre-Web destination is available.")

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        raise OSError("Unix sockets are not permitted for Calibre-Web connections.")

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


class CalibreWebError(Exception):
    """Sanitized CWNG communication or authorization error."""


def normalize_calibre_web_url(value: str) -> str:
    """Validate a public HTTPS CWNG origin and return its normalized URL.

    CWNG is reached through the public HTTPS reverse-proxy route. Rejecting
    literal/private hosts and local suffixes prevents this per-user setting
    from becoming an obvious server-side request forgery primitive.
    """
    raw = value.strip()
    parts = urlsplit(raw)
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Calibre-Web URL must be a public HTTPS URL without credentials or query parameters.")

    host = parts.hostname.rstrip(".").lower()
    if host in {"localhost"} or host.endswith((".localhost", ".local", ".lan", ".internal", ".home")):
        raise ValueError("Calibre-Web URL must use a publicly resolvable hostname.")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        raise ValueError("Use the public hostname for Calibre-Web, not an IP address.")

    try:
        port = parts.port or 443
    except ValueError as exc:
        raise ValueError("Calibre-Web URL contains an invalid port.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Calibre-Web URL contains an invalid port.")

    _resolve_public_addresses(host, port)

    path = parts.path.rstrip("/")
    return urlunsplit(("https", parts.netloc, path, "", ""))


class CalibreWebClient:
    """A CWNG session client backed by one LibrisLog connection record."""

    def __init__(
        self,
        connection: CalibreWebConnection,
        session: Session,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.connection = connection
        self.session = session
        self.transport = transport
        self.http: httpx.AsyncClient | None = None
        self.csrf_token: str | None = None
        self.me: dict = {}

    async def __aenter__(self) -> "CalibreWebClient":
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def open(self) -> None:
        if not self.connection.is_enabled:
            raise CalibreWebError("Calibre-Web connection is disabled.")
        try:
            self.connection.base_url = normalize_calibre_web_url(self.connection.base_url)
        except ValueError as exc:
            raise CalibreWebError("The configured Calibre-Web URL is no longer considered safe.") from exc

        transport = self.transport
        if transport is None:
            parts = urlsplit(self.connection.base_url)
            host = parts.hostname
            if not host:
                raise CalibreWebError("The configured Calibre-Web URL has no hostname.")
            try:
                port = parts.port or 443
                pinned_addresses = _resolve_public_addresses(host, port)
            except ValueError as exc:
                raise CalibreWebError("The configured Calibre-Web hostname is no longer considered safe.") from exc

            # HTTPX does not expose httpcore's network_backend constructor
            # argument. Install the pinned backend on its standard transport
            # and fail closed if a future HTTPX/httpcore version changes it.
            transport = httpx.AsyncHTTPTransport(trust_env=False)
            pool = getattr(transport, "_pool", None)
            if pool is None or not hasattr(pool, "_network_backend"):
                await transport.aclose()
                raise CalibreWebError("The installed HTTP transport cannot enforce pinned DNS safely.")
            pool._network_backend = _PinnedDNSBackend(host, port, pinned_addresses)

        self.http = httpx.AsyncClient(
            base_url=self.connection.base_url.rstrip("/") + "/",
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

        if self.connection.session_cookie_encrypted:
            try:
                cookie_header = decrypt_api_key(self.connection.session_cookie_encrypted)
                host = urlsplit(self.connection.base_url).hostname
                for item in cookie_header.split(";"):
                    name, separator, value = item.strip().partition("=")
                    if separator and name and host:
                        self.http.cookies.set(name, value, domain=host, path="/")
                await self._refresh_csrf()
                response = await self._request("GET", "api/v1/auth/me")
                if response.status_code == 200:
                    self.me = self._json_object(response)
                    self._persist_session()
                    return
            except (CalibreWebError, ValueError, InvalidToken):
                # Expired/invalid session: fall back to a fresh login below.
                pass

        await self._login()

    async def close(self) -> None:
        if self.http is not None:
            await self.http.aclose()
            self.http = None

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self.http is None:
            raise CalibreWebError("Calibre-Web client is not open.")
        try:
            response = await self.http.request(method, path.lstrip("/"), **kwargs)
            # Keep an explicit Cookie header in step with Set-Cookie updates
            # because this client persists its session across requests.
            cookie_header = self._cookie_header()
            if cookie_header:
                self.http.headers["Cookie"] = cookie_header
        except httpx.TimeoutException as exc:
            raise CalibreWebError("Calibre-Web did not respond before the timeout.") from exc
        except httpx.HTTPError as exc:
            raise CalibreWebError("Could not communicate with Calibre-Web.") from exc

        # Do not follow redirects: a redirect could send credentials to a
        # different origin or conceal a reverse-proxy/login failure.
        if 300 <= response.status_code < 400:
            raise CalibreWebError("Calibre-Web returned an unexpected redirect. Check its public URL and proxy route.")
        return response

    @staticmethod
    def _json_object(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except ValueError as exc:
            raise CalibreWebError("Calibre-Web returned an invalid response.") from exc
        if not isinstance(payload, dict):
            raise CalibreWebError("Calibre-Web returned an unexpected response.")
        return payload

    def _cookie_header(self) -> str:
        if self.http is None:
            return ""
        return "; ".join(
            f"{cookie.name}={cookie.value}"
            for cookie in self.http.cookies.jar
        )

    def _persist_session(self) -> None:
        if self.http is None or not self.csrf_token:
            return
        cookie_header = self._cookie_header()
        if not cookie_header:
            raise CalibreWebError("Calibre-Web did not issue a session cookie.")
        self.connection.session_cookie_encrypted = encrypt_api_key(cookie_header)
        self.session.add(self.connection)

    async def _refresh_csrf(self) -> None:
        response = await self._request("GET", "api/v1/auth/csrf")
        if response.status_code != 200:
            raise CalibreWebError("Could not obtain a Calibre-Web CSRF token.")
        payload = self._json_object(response)
        token = payload.get("csrf_token")
        if not isinstance(token, str) or not token:
            raise CalibreWebError("Calibre-Web did not provide a CSRF token.")
        self.csrf_token = token

    async def _login(self) -> None:
        await self._refresh_csrf()
        try:
            password = decrypt_api_key(self.connection.password_encrypted)
        except InvalidToken as exc:
            raise CalibreWebError(
                "The saved Calibre-Web password cannot be decrypted. Reconnect the account."
            ) from exc
        response = await self._request(
            "POST",
            "api/v1/auth/login",
            json={
                "username": self.connection.username,
                "password": password,
            },
            headers={
                "X-CSRFToken": self.csrf_token,
                "X-CSRF-Token": self.csrf_token,
                "Accept": "application/json",
            },
        )
        if response.status_code != 200:
            # Never include the request body or upstream response in logs/errors.
            raise CalibreWebError("Calibre-Web login failed. Check the username and password.")
        self.me = self._json_object(response)
        if not isinstance(self.me.get("role"), dict):
            raise CalibreWebError("Calibre-Web login response did not contain account permissions.")
        await self._refresh_csrf()
        self._persist_session()
        self.connection.last_verified_at = datetime.now(timezone.utc)
        self.session.add(self.connection)

    def require_upload_permission(self) -> None:
        role = self.me.get("role") or {}
        features = self.me.get("features") or {}
        if not role.get("upload"):
            raise CalibreWebError("The mapped Calibre-Web account does not have upload permission.")
        if not features.get("uploading"):
            raise CalibreWebError("Uploads are disabled in Calibre-Web server settings.")

    def require_edit_permission(self) -> None:
        role = self.me.get("role") or {}
        if not role.get("edit"):
            raise CalibreWebError("The mapped Calibre-Web account does not have metadata edit permission.")

    async def get_tasks(self) -> list[dict]:
        response = await self._request("GET", "api/v1/tasks")
        if response.status_code != 200:
            raise CalibreWebError("Could not read Calibre-Web upload tasks.")
        items = self._json_object(response).get("items", [])
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def upload_new_book(self, filename: str, data: bytes | BinaryIO) -> dict:
        self.require_upload_permission()
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        response = await self._request(
            "POST",
            "api/v1/upload",
            files={"file": (filename, data, content_type)},
            headers={
                "X-CSRFToken": self.csrf_token or "",
                "X-CSRF-Token": self.csrf_token or "",
                "Accept": "application/json",
            },
        )
        if response.status_code != 200:
            raise CalibreWebError("Calibre-Web rejected the ebook upload.")
        return self._json_object(response)

    async def add_book_format(self, calibre_book_id: int, filename: str, data: bytes | BinaryIO) -> dict:
        self.require_upload_permission()
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        response = await self._request(
            "POST",
            f"api/v1/books/{calibre_book_id}/formats",
            files={"file": (filename, data, content_type)},
            headers={
                "X-CSRFToken": self.csrf_token or "",
                "X-CSRF-Token": self.csrf_token or "",
                "Accept": "application/json",
            },
        )
        if response.status_code not in (200, 202):
            raise CalibreWebError("Calibre-Web rejected the format upload.")
        return self._json_object(response)

    async def get_book(self, calibre_book_id: int) -> dict:
        response = await self._request("GET", f"api/v1/books/{calibre_book_id}")
        if response.status_code == 404:
            raise CalibreWebError("The selected Calibre-Web book does not exist or is not accessible.")
        if response.status_code != 200:
            raise CalibreWebError("Could not read the selected Calibre-Web book.")
        return self._json_object(response)

    async def search_books(self, query: str, limit: int = 20) -> list[dict]:
        response = await self._request(
            "GET",
            "api/v1/books",
            params={"search": query, "per_page": min(max(limit, 1), 50)},
        )
        if response.status_code != 200:
            raise CalibreWebError("Could not search the Calibre-Web library.")
        items = self._json_object(response).get("items", [])
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def update_book_metadata(self, calibre_book_id: int, metadata: dict) -> dict:
        self.require_edit_permission()
        response = await self._request(
            "POST",
            f"api/v1/books/{calibre_book_id}/metadata",
            json=metadata,
            headers={
                "X-CSRFToken": self.csrf_token or "",
                "X-CSRF-Token": self.csrf_token or "",
                "Accept": "application/json",
            },
        )
        if response.status_code != 200:
            raise CalibreWebError("Calibre-Web rejected the metadata update.")
        return self._json_object(response)
