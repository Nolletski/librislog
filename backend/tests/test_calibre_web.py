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
from app.models import Book, CalibreWebConnection, CalibreWebUpload, User
from sqlmodel import select
from app.routers.calibre_web import (
    _apply_task_status,
    _book_formats,
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
        "name": "Reader One",
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
    with pytest.raises(ValueError, match="invalid port"):
        normalize_calibre_web_url("https://ebooks.example.com:invalid")


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
            assert client.me["name"] == "Reader One"
            assert client.me["role"]["upload"] is True

        cookie = decrypt_api_key(connection.session_cookie_encrypted or "")
        assert cookie == "session=authenticated"

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
        {"task_id": "old", "user": "Reader One", "taskMessage": "Upload: book.epub"},
        {"task_id": "wrong-user", "user": "Administrator", "taskMessage": "Upload done: book.epub"},
        {"task_id": "new", "user": "Reader One", "taskMessage": "Upload done: book.epub"},
        {"task_id": "other", "user": "Reader One", "taskMessage": "Upload done: another.epub"},
    ]
    # CWNG task.user is serialize_user(user).name, not the login username.
    match = _task_for_upload(tasks, {"old", "other"}, "book.epub", "Reader One")
    assert match is not None
    assert match["task_id"] == "new"


def test_upload_task_correlation_refuses_ambiguous_candidates() -> None:
    tasks = [
        {"task_id": "new-1", "user": "Reader One", "taskMessage": "Upload done: book.epub"},
        {"task_id": "new-2", "user": "Reader One", "taskMessage": "Upload done: book.epub"},
    ]
    assert _task_for_upload(tasks, set(), "book.epub", "Reader One") is None


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


def test_cw_book_formats_accepts_serialized_format_objects() -> None:
    assert _book_formats({
        "formats": [
            {"format": "EPUB", "size_bytes": 100},
            {"format": "PDF", "size_bytes": 200},
            "MOBI",
            {"other": "ignored"},
        ]
    }) == {"EPUB", "PDF", "MOBI"}


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



def test_catalogue_projection_does_not_clear_unknown_remote_fields() -> None:
    book = Book(title="Example", publisher=None, published_year=None, blurb=None, isbn=None)
    payload = _metadata_payload(book, [])
    assert payload == {"title": "Example"}


def test_catalogue_projection_updates_only_a_matching_integer_pages_column() -> None:
    book = Book(title="Example", page_count=352)
    payload = _metadata_payload(
        book,
        [],
        [],
        [
            {"id": 3, "name": "pages", "label": "Pages", "datatype": "int"},
            {"id": 4, "name": "status", "label": "Status", "datatype": "text"},
        ],
    )
    assert payload["custom_column_3"] == "352"
    assert "custom_column_4" not in payload


def test_catalogue_projection_does_not_clear_pages_when_page_count_is_unknown() -> None:
    book = Book(title="Example", page_count=0)
    payload = _metadata_payload(
        book,
        [],
        [],
        [{"id": 3, "name": "pages", "label": "Pages", "datatype": "int"}],
    )
    assert not any(key.startswith("custom_column_") for key in payload)


def test_catalogue_projection_updates_isbn_without_dropping_other_identifiers() -> None:
    book = Book(title="Example", isbn="9781234567890")
    payload = _metadata_payload(
        book,
        ["Author"],
        [
            {"type": "asin", "val": "B000000000"},
            {"type": "isbn", "val": "9780000000000"},
        ],
    )
    assert payload["identifiers"] == [
        {"type": "asin", "val": "B000000000"},
        {"type": "isbn", "val": "9781234567890"},
    ]


def test_calibre_web_upload_and_book_routes_are_user_scoped(client, session, create_user_with_key) -> None:
    owner = session.exec(select(User).where(User.email == "test@example.com")).first()
    assert owner is not None and owner.id is not None
    other_user, other_key = create_user_with_key(email="other@example.com")
    assert other_user.id is not None

    owner_connection = CalibreWebConnection(
        user_id=owner.id,
        base_url="https://ebooks.owner.example",
        username="owner",
        password_encrypted=encrypt_api_key("owner-secret"),
    )
    other_connection = CalibreWebConnection(
        user_id=other_user.id,
        base_url="https://ebooks.other.example",
        username="other",
        password_encrypted=encrypt_api_key("other-secret"),
    )
    owner_book = Book(title="Private book", user_id=owner.id)
    session.add_all([owner_connection, other_connection, owner_book])
    session.commit()
    session.refresh(owner_connection)
    session.refresh(owner_book)
    assert owner_connection.id is not None and owner_book.id is not None

    upload = CalibreWebUpload(
        connection_id=owner_connection.id,
        librislog_book_id=owner_book.id,
        filename="private.epub",
        file_format="EPUB",
        status="queued",
    )
    session.add(upload)
    session.commit()
    session.refresh(upload)
    assert upload.id is not None

    client.headers["X-API-Key"] = other_key
    assert client.get(f"/api/calibre-web/books/{owner_book.id}/files").status_code == 404
    assert client.get(f"/api/calibre-web/uploads/{upload.id}").status_code == 404


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
