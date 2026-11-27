"""Tests for the Calibre-Web NextGen integration boundary."""

import asyncio
import json
import socket
import zipfile
from io import BytesIO
from fastapi import UploadFile

import httpx
import pytest

from app.auth import decrypt_api_key, encrypt_api_key
from app.models import Book, CalibreWebConnection
from app.routers.calibre_web import (
    _apply_task_status,
    _metadata_payload,
    _task_for_upload,
    _validate_ebook_file,
)
from app.services.calibre_web_client import (
    CalibreWebClient,
    CalibreWebError,
    normalize_calibre_web_url,
)


class _SessionStub:
    def add(self, _model) -> None:
        pass


def _auth_payload() -> dict:
    return {
        "role": {"upload": True, "edit": True},
        "features": {"uploading": True},
    }


def test_normalize_calibre_web_url_requires_public_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    assert normalize_calibre_web_url("https://ebooks.example.com/") == "https://ebooks.example.com"

    with pytest.raises(ValueError):
        normalize_calibre_web_url("http://ebooks.example.com")
    with pytest.raises(ValueError):
        normalize_calibre_web_url("https://127.0.0.1")


def test_normalize_calibre_web_url_rejects_private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 443))],
    )
    with pytest.raises(ValueError, match="public IP"):
        normalize_calibre_web_url("https://ebooks.example.com")


def test_cw_client_logs_in_and_reuses_encrypted_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/auth/csrf"):
            return httpx.Response(
                200,
                json={"csrf_token": "csrf-test-token"},
                headers={"set-cookie": "session=authenticated; Path=/; HttpOnly; Secure"},
            )
        if request.url.path.endswith("/auth/login"):
            payload = json.loads(request.content.decode("utf-8"))
            assert payload == {"username": "reader", "password": "secret"}
            assert request.headers.get("x-csrftoken") == "csrf-test-token"
            return httpx.Response(
                200,
                json=_auth_payload(),
                headers={"set-cookie": "session=authenticated; Path=/; HttpOnly; Secure"},
            )
        if request.url.path.endswith("/auth/me"):
            assert "session=authenticated" in request.headers.get("cookie", "")
            return httpx.Response(200, json=_auth_payload())
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    connection = CalibreWebConnection(
        user_id=1,
        base_url="https://ebooks.example.com",
        username="reader",
        password_encrypted=encrypt_api_key("secret"),
    )

    async def run() -> None:
        async with CalibreWebClient(
            connection,
            _SessionStub(),  # type: ignore[arg-type]
            transport=httpx.MockTransport(handler),
        ) as client:
            client.require_upload_permission()
            client.require_edit_permission()
            assert client.me["role"]["upload"] is True

        cookie = decrypt_api_key(connection.session_cookie_encrypted or "")
        assert cookie == "session=authenticated"
        assert connection.csrf_token_encrypted is not None

        first_login_count = sum(request.url.path.endswith("/auth/login") for request in requests)
        assert first_login_count == 1

        # A second client restores the encrypted cookie and does not hit login.
        async with CalibreWebClient(
            connection,
            _SessionStub(),  # type: ignore[arg-type]
            transport=httpx.MockTransport(handler),
        ) as client:
            assert client.me["role"]["edit"] is True

        assert sum(request.url.path.endswith("/auth/login") for request in requests) == 1

    asyncio.run(run())


def test_cw_client_refuses_account_without_upload_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/csrf"):
            return httpx.Response(
                200,
                json={"csrf_token": "csrf-test-token"},
                headers={"set-cookie": "session=authenticated; Path=/; HttpOnly; Secure"},
            )
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={
                "role": {"upload": False, "edit": True},
                "features": {"uploading": True},
            })
        if request.url.path.endswith("/auth/me"):
            return httpx.Response(200, json={
                "role": {"upload": False, "edit": True},
                "features": {"uploading": True},
            })
        raise AssertionError(f"Unexpected request: {request.url}")

    connection = CalibreWebConnection(
        user_id=1,
        base_url="https://ebooks.example.com",
        username="reader",
        password_encrypted=encrypt_api_key("secret"),
    )

    async def run() -> None:
        async with CalibreWebClient(
            connection,
            _SessionStub(),  # type: ignore[arg-type]
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(CalibreWebError, match="upload permission"):
                client.require_upload_permission()

    asyncio.run(run())


def test_upload_task_correlation_uses_new_task_and_filename() -> None:
    tasks = [
        {"task_id": "old", "taskMessage": "Upload: book.epub"},
        {"task_id": "new", "taskMessage": "Upload done: book.epub"},
        {"task_id": "other", "taskMessage": "Upload done: another.epub"},
    ]
    match = _task_for_upload(tasks, {"old", "other"}, "book.epub")
    assert match is not None
    assert match["task_id"] == "new"


@pytest.mark.parametrize(
    ("stat_value", "expected"),
    [
        (0, "queued"),
        (2, "processing"),
        (3, "needs_match"),
        (1, "failed"),
        (4, "failed"),
        (5, "failed"),
    ],
)
def test_upload_status_mapping(stat_value: int, expected: str) -> None:
    from app.models import CalibreWebUpload

    upload = CalibreWebUpload(
        connection_id=1,
        librislog_book_id=1,
        filename="book.epub",
        file_format="EPUB",
    )
    _apply_task_status(upload, {"task_id": "task-1", "stat": stat_value})
    assert upload.status == expected


def test_catalogue_projection_excludes_reading_data() -> None:
    book = Book(
        title="Example",
        publisher="Publisher",
        published_year=2024,
        blurb="Public description",
        notes="Private note",
        rating=5,
    )
    payload = _metadata_payload(book, ["Author One", "Author Two"])
    assert payload == {
        "title": "Example",
        "authors": "Author One & Author Two",
        "publishers": "Publisher",
        "pubdate": "2024-01-01",
        "comments": "Public description",
    }
    assert "rating" not in payload
    assert "notes" not in payload
    assert "reading_status" not in payload



def test_ebook_upload_checks_pdf_and_epub_signatures() -> None:
    pdf = UploadFile(filename="valid.pdf", file=BytesIO(b"%PDF-1.7 sample"))
    invalid_pdf = UploadFile(filename="fake.pdf", file=BytesIO(b"not a pdf"))
    assert _validate_ebook_file(pdf, ".pdf") is True
    assert _validate_ebook_file(invalid_pdf, ".pdf") is False

    epub_bytes = BytesIO()
    with zipfile.ZipFile(epub_bytes, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("OEBPS/content.opf", "<package/>")
    epub_bytes.seek(0)
    epub = UploadFile(filename="valid.epub", file=epub_bytes)
    invalid_epub = UploadFile(filename="fake.epub", file=BytesIO(b"not a zip"))
    assert _validate_ebook_file(epub, ".epub") is True
    assert _validate_ebook_file(invalid_epub, ".epub") is False
