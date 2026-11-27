"""Tests for app.services.user_deletion module."""

from pathlib import Path

from app.auth import get_password_hash
from app.models import Book, CalibreWebBookLink, CalibreWebConnection, CalibreWebUpload, User, UserRole
from app.services.user_deletion import delete_user_account_data, delete_user_reading_data
from sqlmodel import Session, select


def test_delete_user_reading_data_cleans_local_covers(session: Session, tmp_path: Path) -> None:
    """Books with local cover URLs should have their cover files deleted."""
    user = User(
        firstname="Test",
        lastname="User",
        email="cover-test@example.com",
        role=UserRole.user,
        hashed_password=get_password_hash("password"),
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    cover_filename = "1__abc123.jpg"
    (tmp_path / cover_filename).write_bytes(b"cover-data")

    book = Book(
        user_id=user.id,
        title="Test Book",
        cover_url="/api/covers/" + cover_filename,
    )
    session.add(book)
    session.commit()

    assert user.id is not None
    counts = delete_user_reading_data(session, user.id, str(tmp_path))

    assert counts.books == 1
    assert not (tmp_path / cover_filename).exists()


def test_delete_user_reading_data_skips_external_covers(session: Session, tmp_path: Path) -> None:
    """Books with external cover URLs should skip cover file deletion."""
    user = User(
        firstname="Test",
        lastname="User",
        email="external-cover@example.com",
        role=UserRole.user,
        hashed_password=get_password_hash("password"),
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    book = Book(
        user_id=user.id,
        title="External Cover Book",
        cover_url="https://example.com/cover.jpg",
    )
    session.add(book)
    session.commit()

    assert user.id is not None    
    counts = delete_user_reading_data(session, user.id, str(tmp_path))

    assert counts.books == 1



def test_reading_data_reset_removes_only_local_cwng_book_records(
    session: Session,
    tmp_path: Path,
) -> None:
    """A reading-data reset clears local links/uploads but preserves the CWNG connection."""
    user = User(
        firstname="Test",
        lastname="Reader",
        email="cwng-reset@example.com",
        role=UserRole.user,
        hashed_password=get_password_hash("password"),
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    book = Book(user_id=user.id, title="Mapped book")
    session.add(book)
    session.commit()
    session.refresh(book)

    connection = CalibreWebConnection(
        user_id=user.id,
        base_url="https://ebooks.example.com",
        username="reader",
        password_encrypted="encrypted-password",
    )
    session.add(connection)
    session.commit()
    session.refresh(connection)

    session.add(CalibreWebBookLink(
        connection_id=connection.id,
        librislog_book_id=book.id,
        calibre_book_id=123,
    ))
    session.add(CalibreWebUpload(
        connection_id=connection.id,
        librislog_book_id=book.id,
        filename="mapped.epub",
        file_format="EPUB",
    ))
    session.commit()

    counts = delete_user_reading_data(session, user.id, str(tmp_path))
    session.commit()

    assert counts.books == 1
    assert session.get(CalibreWebConnection, connection.id) is not None
    assert session.exec(
        select(CalibreWebBookLink).where(CalibreWebBookLink.connection_id == connection.id)
    ).first() is None
    assert session.exec(
        select(CalibreWebUpload).where(CalibreWebUpload.connection_id == connection.id)
    ).first() is None


def test_account_deletion_removes_local_cwng_credentials_without_remote_delete(
    session: Session,
    tmp_path: Path,
) -> None:
    """Deleting a LibrisLog account removes its local CWNG connection only."""
    user = User(
        firstname="Test",
        lastname="Reader",
        email="cwng-delete@example.com",
        role=UserRole.user,
        hashed_password=get_password_hash("password"),
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    connection = CalibreWebConnection(
        user_id=user.id,
        base_url="https://ebooks.example.com",
        username="reader",
        password_encrypted="encrypted-password",
    )
    session.add(connection)
    session.commit()
    session.refresh(connection)

    delete_user_account_data(session, user, str(tmp_path))
    session.commit()

    assert session.get(User, user.id) is None
    assert session.get(CalibreWebConnection, connection.id) is None
