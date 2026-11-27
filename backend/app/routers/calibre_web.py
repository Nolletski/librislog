"""Calibre-Web NextGen connection, ebook upload and explicit book linking API."""

from __future__ import annotations

import html
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.auth import encrypt_api_key, require_user
from app.database import get_session
from app.models import (
    Book,
    CalibreWebBookLink,
    CalibreWebConnection,
    CalibreWebUpload,
    User,
)
from app.services.authors import load_authors_batch
from app.services.calibre_web_client import (
    CalibreWebClient,
    CalibreWebError,
    normalize_calibre_web_url,
)

router = APIRouter(prefix="/api/calibre-web", tags=["calibre-web"])

MAX_EBOOK_SIZE_BYTES = 100 * 1024 * 1024
ALLOWED_EBOOK_FORMATS = {".epub", ".pdf"}


class ConnectionConfigureRequest(BaseModel):
    base_url: str = PydanticField(min_length=8, max_length=500)
    username: str = PydanticField(min_length=1, max_length=255)
    password: str = PydanticField(min_length=1, max_length=1024)


class ConnectionRead(BaseModel):
    configured: bool
    base_url: Optional[str] = None
    username: Optional[str] = None
    is_enabled: bool = False
    last_verified_at: Optional[datetime] = None


class ConnectionTestResponse(ConnectionRead):
    can_upload: bool = False
    can_edit_metadata: bool = False


class UploadRead(BaseModel):
    id: int
    filename: str
    file_format: str
    status: str
    calibre_task_id: Optional[str] = None
    calibre_book_id: Optional[int] = None
    error_message: Optional[str] = None
    metadata_sync_error: Optional[str] = None
    created_at: datetime


class BookFilesResponse(BaseModel):
    configured: bool
    linked: bool = False
    calibre_book_id: Optional[int] = None
    library_id: Optional[str] = None
    metadata_synced_at: Optional[datetime] = None
    uploads: list[UploadRead] = PydanticField(default_factory=list)


class BookMatchRequest(BaseModel):
    calibre_book_id: int = PydanticField(gt=0)
    library_id: str = PydanticField(default="default", min_length=1, max_length=255)


class MetadataSyncResponse(BaseModel):
    synced: bool
    message: Optional[str] = None


def _get_connection(session: Session, user_id: int) -> CalibreWebConnection:
    connection = session.exec(
        select(CalibreWebConnection).where(CalibreWebConnection.user_id == user_id)
    ).first()
    if connection is None:
        raise HTTPException(status_code=404, detail="Calibre-Web is not connected.")
    return connection


def _connection_read(connection: CalibreWebConnection | None) -> ConnectionRead:
    if connection is None:
        return ConnectionRead(configured=False)
    return ConnectionRead(
        configured=True,
        base_url=connection.base_url,
        username=connection.username,
        is_enabled=connection.is_enabled,
        last_verified_at=connection.last_verified_at,
    )


def _upload_read(
    upload: CalibreWebUpload,
    metadata_sync_error: str | None = None,
) -> UploadRead:
    return UploadRead(
        id=upload.id or 0,
        filename=upload.filename,
        file_format=upload.file_format,
        status=upload.status,
        calibre_task_id=upload.calibre_task_id,
        calibre_book_id=upload.calibre_book_id,
        error_message=upload.error_message,
        metadata_sync_error=metadata_sync_error,
        created_at=upload.created_at,
    )


def _task_for_upload(
    tasks: list[dict],
    before_ids: set[str],
    filename: str,
) -> dict | None:
    escaped_filename = html.escape(filename)
    for task in tasks:
        task_id = str(task.get("task_id", ""))
        message = str(task.get("taskMessage") or "")
        if task_id and task_id not in before_ids and (
            filename in message or escaped_filename in message
        ):
            return task
    return None


def _apply_task_status(upload: CalibreWebUpload, task: dict) -> None:
    upload.calibre_task_id = str(task.get("task_id") or upload.calibre_task_id or "")
    stat_value = task.get("stat")
    if stat_value == 0:
        upload.status = "queued"
    elif stat_value == 2:
        upload.status = "processing"
    elif stat_value == 3:
        upload.status = "needs_match" if upload.calibre_book_id is None else "completed"
        upload.error_message = None
    elif stat_value in (1, 4, 5):
        upload.status = "failed"
        error = task.get("error")
        upload.error_message = str(error)[:2000] if error else "Calibre-Web upload task did not complete."
    upload.updated_at = datetime.now(timezone.utc)


def _validate_ebook_file(file: UploadFile, extension: str) -> bool:
    """Check the container signature rather than trusting the supplied filename."""
    try:
        file.file.seek(0)
        if extension == ".pdf":
            return file.file.read(5) == b"%PDF-"
        if extension == ".epub":
            with zipfile.ZipFile(file.file) as archive:
                if "mimetype" not in archive.namelist():
                    return False
                return archive.read("mimetype") == b"application/epub+zip"
        return False
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return False
    finally:
        file.file.seek(0)


def _metadata_payload(
    book: Book,
    authors: list[str],
    existing_identifiers: list[dict] | None = None,
) -> dict:
    """Project only catalogue metadata owned by LibrisLog.

    Reading status, dates, ratings, personal notes, page progress and acquisition
    state deliberately never leave LibrisLog.
    """
    payload = {
        "title": book.title,
        "authors": " & ".join(authors),
        "publishers": book.publisher or "",
        "pubdate": f"{book.published_year}-01-01" if book.published_year else "",
        "comments": book.blurb or "",
    }
    if book.isbn:
        # Keep CWNG identifiers such as ASIN/Goodreads intact while replacing
        # only the ISBN with LibrisLog's current catalogue value.
        identifiers = [
            item for item in (existing_identifiers or [])
            if isinstance(item, dict) and str(item.get("type", "")).lower() != "isbn"
        ]
        identifiers.append({"type": "isbn", "val": book.isbn})
        payload["identifiers"] = identifiers
    return payload


async def _sync_metadata(
    client: CalibreWebClient,
    session: Session,
    book: Book,
    link: CalibreWebBookLink,
) -> None:
    client.require_edit_permission()
    assert book.id is not None
    authors_map = load_authors_batch(session, [book.id])
    authors = authors_map.get(book.id) or []
    existing_book = await client.get_book(link.calibre_book_id)
    existing_identifiers = existing_book.get("identifiers", [])
    result = await client.update_book_metadata(
        link.calibre_book_id,
        _metadata_payload(
            book,
            authors,
            existing_identifiers if isinstance(existing_identifiers, list) else [],
        ),
    )
    errors = result.get("errors")
    if errors:
        raise CalibreWebError("Calibre-Web could not update every catalogue field.")
    link.metadata_synced_at = datetime.now(timezone.utc)
    session.add(link)


@router.get("/connection", response_model=ConnectionRead)
def get_connection(
    current_user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> ConnectionRead:
    connection = session.exec(
        select(CalibreWebConnection).where(CalibreWebConnection.user_id == current_user.id)
    ).first()
    return _connection_read(connection)


@router.post("/connection", response_model=ConnectionTestResponse)
async def configure_connection(
    body: ConnectionConfigureRequest,
    current_user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> ConnectionTestResponse:
    assert current_user.id is not None
    try:
        base_url = normalize_calibre_web_url(body.base_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    connection = session.exec(
        select(CalibreWebConnection).where(CalibreWebConnection.user_id == current_user.id)
    ).first()
    if connection is None:
        connection = CalibreWebConnection(
            user_id=current_user.id,
            base_url=base_url,
            username=body.username.strip(),
            password_encrypted=encrypt_api_key(body.password),
        )
    else:
        connection.base_url = base_url
        connection.username = body.username.strip()
        connection.password_encrypted = encrypt_api_key(body.password)
        connection.session_cookie_encrypted = None
        connection.is_enabled = True
        connection.updated_at = datetime.now(timezone.utc)

    session.add(connection)
    try:
        async with CalibreWebClient(connection, session) as client:
            client.require_upload_permission()
            client.require_edit_permission()
            session.commit()
            return ConnectionTestResponse(
                configured=True,
                base_url=connection.base_url,
                username=connection.username,
                is_enabled=connection.is_enabled,
                last_verified_at=connection.last_verified_at,
                can_upload=True,
                can_edit_metadata=True,
            )
    except CalibreWebError as exc:
        session.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/connection", status_code=204)
def disconnect(
    current_user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> None:
    connection = session.exec(
        select(CalibreWebConnection).where(CalibreWebConnection.user_id == current_user.id)
    ).first()
    if connection is None:
        return
    for link in session.exec(
        select(CalibreWebBookLink).where(CalibreWebBookLink.connection_id == connection.id)
    ).all():
        session.delete(link)
    for upload in session.exec(
        select(CalibreWebUpload).where(CalibreWebUpload.connection_id == connection.id)
    ).all():
        session.delete(upload)
    session.delete(connection)
    session.commit()


@router.get("/books/{book_id}/files", response_model=BookFilesResponse)
def get_book_files(
    book_id: int,
    current_user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> BookFilesResponse:
    book = session.get(Book, book_id)
    if book is None or book.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Book not found.")

    connection = session.exec(
        select(CalibreWebConnection).where(CalibreWebConnection.user_id == current_user.id)
    ).first()
    if connection is None:
        return BookFilesResponse(configured=False)

    link = session.exec(
        select(CalibreWebBookLink).where(
            CalibreWebBookLink.connection_id == connection.id,
            CalibreWebBookLink.librislog_book_id == book_id,
        )
    ).first()
    uploads = session.exec(
        select(CalibreWebUpload)
        .where(
            CalibreWebUpload.connection_id == connection.id,
            CalibreWebUpload.librislog_book_id == book_id,
        )
        .order_by(CalibreWebUpload.created_at.desc())
    ).all()
    return BookFilesResponse(
        configured=True,
        linked=link is not None,
        calibre_book_id=link.calibre_book_id if link else None,
        library_id=link.library_id if link else None,
        metadata_synced_at=link.metadata_synced_at if link else None,
        uploads=[_upload_read(upload) for upload in uploads],
    )


@router.get("/books/search")
async def search_calibre_books(
    q: str = Query(min_length=2, max_length=200),
    current_user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> dict:
    connection = _get_connection(session, current_user.id)
    try:
        async with CalibreWebClient(connection, session) as client:
            items = await client.search_books(q)
        session.commit()
        return {"items": items}
    except CalibreWebError as exc:
        session.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/books/{book_id}/upload", response_model=UploadRead, status_code=202)
async def upload_ebook(
    book_id: int,
    file: UploadFile = File(...),
    replace_existing: bool = Form(False),
    current_user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> UploadRead:
    assert current_user.id is not None
    book = session.get(Book, book_id)
    if book is None or book.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Book not found.")

    # Treat both POSIX and Windows separators as path separators. The remote
    # library should receive only a plain filename, never a client-supplied path.
    supplied_filename = (file.filename or "").replace("\\", "/")
    filename = Path(supplied_filename).name
    extension = Path(filename).suffix.lower()
    if not filename or len(filename) > 255 or extension not in ALLOWED_EBOOK_FORMATS:
        raise HTTPException(status_code=415, detail="Only EPUB and PDF files are supported.")
    file.file.seek(0, 2)
    file_size = file.file.tell()
    file.file.seek(0)
    if file_size == 0:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if file_size > MAX_EBOOK_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="The ebook exceeds the 100 MB upload limit.")
    if not _validate_ebook_file(file, extension):
        raise HTTPException(status_code=415, detail="The file content does not match a valid EPUB or PDF.")

    connection = _get_connection(session, current_user.id)
    pending_same_name = session.exec(
        select(CalibreWebUpload).where(
            CalibreWebUpload.connection_id == connection.id,
            CalibreWebUpload.filename == filename,
            CalibreWebUpload.status.in_(["queued", "processing"]),
        )
    ).first()
    if pending_same_name:
        raise HTTPException(
            status_code=409,
            detail="An upload with this filename is still processing. Wait for it to finish before retrying.",
        )

    link = session.exec(
        select(CalibreWebBookLink).where(
            CalibreWebBookLink.connection_id == connection.id,
            CalibreWebBookLink.librislog_book_id == book_id,
        )
    ).first()

    try:
        async with CalibreWebClient(connection, session) as client:
            client.require_upload_permission()
            before_tasks = await client.get_tasks()
            before_ids = {
                str(task.get("task_id"))
                for task in before_tasks
                if task.get("task_id") is not None
            }
            if link:
                existing_book = await client.get_book(link.calibre_book_id)
                existing_formats = {
                    str(item).upper()
                    for item in existing_book.get("formats", [])
                    if isinstance(item, str)
                }
                file_format = extension.lstrip(".").upper()
                if file_format in existing_formats and not replace_existing:
                    raise HTTPException(
                        status_code=409,
                        detail=f"This book already has a {file_format} format. Confirm replacement to overwrite it.",
                    )
                result = await client.add_book_format(link.calibre_book_id, filename, file.file)
                queued = result.get("queued")
                if queued != filename:
                    raise CalibreWebError("Calibre-Web did not confirm the format upload.")
                calibre_book_id = link.calibre_book_id
            else:
                result = await client.upload_new_book(filename, file.file)
                queued = result.get("queued") or []
                errors = result.get("errors") or []
                if filename not in queued:
                    message = errors[0].get("error") if errors and isinstance(errors[0], dict) else None
                    raise CalibreWebError(message or "Calibre-Web did not queue the ebook.")
                calibre_book_id = None

            tasks = await client.get_tasks()
            task = _task_for_upload(tasks, before_ids, filename)
            upload = CalibreWebUpload(
                connection_id=connection.id,
                librislog_book_id=book_id,
                filename=filename,
                file_format=extension.lstrip(".").upper(),
                status="queued",
                task_ids_before_json=json.dumps(sorted(before_ids)),
                calibre_task_id=str(task.get("task_id")) if task else None,
                calibre_book_id=calibre_book_id,
            )
            if task:
                _apply_task_status(upload, task)
            session.add(upload)
            session.commit()
            session.refresh(upload)
            return _upload_read(upload)
    except CalibreWebError as exc:
        session.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/uploads/{upload_id}", response_model=UploadRead)
async def get_upload_status(
    upload_id: int,
    current_user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> UploadRead:
    connection = _get_connection(session, current_user.id)
    upload = session.get(CalibreWebUpload, upload_id)
    if upload is None or upload.connection_id != connection.id:
        raise HTTPException(status_code=404, detail="Upload not found.")

    if upload.status in {"queued", "processing"}:
        try:
            async with CalibreWebClient(connection, session) as client:
                tasks = await client.get_tasks()
                if not upload.calibre_task_id:
                    before_ids = set(json.loads(upload.task_ids_before_json or "[]"))
                    task = _task_for_upload(tasks, before_ids, upload.filename)
                    if task:
                        _apply_task_status(upload, task)
                else:
                    task = next(
                        (item for item in tasks if str(item.get("task_id")) == upload.calibre_task_id),
                        None,
                    )
                    if task:
                        _apply_task_status(upload, task)
            session.add(upload)
            session.commit()
        except (CalibreWebError, ValueError, json.JSONDecodeError) as exc:
            session.rollback()
            if isinstance(exc, CalibreWebError):
                raise HTTPException(status_code=502, detail=str(exc)) from exc

    return _upload_read(upload)


@router.post("/uploads/{upload_id}/match", response_model=UploadRead)
async def match_uploaded_book(
    upload_id: int,
    body: BookMatchRequest,
    current_user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> UploadRead:
    assert current_user.id is not None
    connection = _get_connection(session, current_user.id)
    upload = session.get(CalibreWebUpload, upload_id)
    if upload is None or upload.connection_id != connection.id:
        raise HTTPException(status_code=404, detail="Upload not found.")
    if upload.status != "needs_match":
        raise HTTPException(status_code=409, detail="The upload must finish before it can be linked.")

    book = session.get(Book, upload.librislog_book_id)
    if book is None or book.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Book not found.")

    existing = session.exec(
        select(CalibreWebBookLink).where(
            CalibreWebBookLink.connection_id == connection.id,
            CalibreWebBookLink.librislog_book_id == book.id,
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="This LibrisLog book is already linked to Calibre-Web.")

    # Verify that the selected ID is actually readable by this CWNG account
    # before persisting a durable mapping. A failed lookup must never create a
    # link that points to an inaccessible or nonexistent book.
    try:
        async with CalibreWebClient(connection, session) as client:
            await client.get_book(body.calibre_book_id)
    except CalibreWebError as exc:
        session.rollback()
        raise HTTPException(
            status_code=422,
            detail="The selected Calibre-Web book does not exist or is not accessible to this account.",
        ) from exc

    link = CalibreWebBookLink(
        connection_id=connection.id,
        librislog_book_id=book.id,
        library_id=body.library_id,
        calibre_book_id=body.calibre_book_id,
    )
    session.add(link)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="This Calibre-Web book is already linked.") from exc

    metadata_message = None
    try:
        async with CalibreWebClient(connection, session) as client:
            await _sync_metadata(client, session, book, link)
        upload.calibre_book_id = body.calibre_book_id
        upload.status = "completed"
        upload.updated_at = datetime.now(timezone.utc)
        session.add(upload)
        session.add(link)
        session.commit()
    except CalibreWebError as exc:
        session.rollback()
        metadata_message = str(exc)
        # Keep the verified explicit link. Metadata can be retried separately.
        upload = session.get(CalibreWebUpload, upload_id)
        if upload:
            upload.calibre_book_id = body.calibre_book_id
            upload.status = "completed"
            session.add(upload)
            session.commit()

    return _upload_read(upload, metadata_sync_error=metadata_message)


@router.post("/books/{book_id}/sync-metadata", response_model=MetadataSyncResponse)
async def sync_book_metadata(
    book_id: int,
    current_user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> MetadataSyncResponse:
    assert current_user.id is not None
    book = session.get(Book, book_id)
    if book is None or book.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Book not found.")
    connection = _get_connection(session, current_user.id)
    link = session.exec(
        select(CalibreWebBookLink).where(
            CalibreWebBookLink.connection_id == connection.id,
            CalibreWebBookLink.librislog_book_id == book_id,
        )
    ).first()
    if link is None:
        raise HTTPException(status_code=404, detail="This book is not linked to Calibre-Web.")

    try:
        async with CalibreWebClient(connection, session) as client:
            await client.get_book(link.calibre_book_id)
            await _sync_metadata(client, session, book, link)
        session.commit()
        return MetadataSyncResponse(synced=True)
    except CalibreWebError as exc:
        session.rollback()
        return MetadataSyncResponse(synced=False, message=str(exc))
