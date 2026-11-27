"""add Calibre-Web NextGen connection, book-link and upload tables

Revision ID: e8f0a1b2c3d4
Revises: c8d1e2f3a4b5
Create Date: 2026-11-27 20:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8f0a1b2c3d4"
down_revision: Union[str, Sequence[str], None] = "c8d1e2f3a4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "calibre_web_connection",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("session_cookie_encrypted", sa.Text(), nullable=True),
        sa.Column("csrf_token_encrypted", sa.Text(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_calibre_web_connection_user_id"),
    )
    op.create_index(
        "ix_calibre_web_connection_user_id",
        "calibre_web_connection",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "calibre_web_book_link",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("librislog_book_id", sa.Integer(), nullable=False),
        sa.Column("library_id", sa.String(length=255), nullable=False),
        sa.Column("calibre_book_id", sa.Integer(), nullable=False),
        sa.Column("linked_at", sa.DateTime(), nullable=False),
        sa.Column("metadata_synced_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["calibre_web_connection.id"]),
        sa.ForeignKeyConstraint(["librislog_book_id"], ["book.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id",
            "librislog_book_id",
            name="uq_calibre_web_book_link_connection_librislog_book",
        ),
        sa.UniqueConstraint(
            "connection_id",
            "library_id",
            "calibre_book_id",
            name="uq_calibre_web_book_link_connection_library_calibre_book",
        ),
    )
    op.create_index(
        "ix_calibre_web_book_link_connection_id",
        "calibre_web_book_link",
        ["connection_id"],
        unique=False,
    )
    op.create_index(
        "ix_calibre_web_book_link_librislog_book_id",
        "calibre_web_book_link",
        ["librislog_book_id"],
        unique=False,
    )
    op.create_index(
        "ix_calibre_web_book_link_calibre_book_id",
        "calibre_web_book_link",
        ["calibre_book_id"],
        unique=False,
    )

    op.create_table(
        "calibre_web_upload",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("librislog_book_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("file_format", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("calibre_task_id", sa.String(length=128), nullable=True),
        sa.Column("calibre_book_id", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["calibre_web_connection.id"]),
        sa.ForeignKeyConstraint(["librislog_book_id"], ["book.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_calibre_web_upload_connection_id",
        "calibre_web_upload",
        ["connection_id"],
        unique=False,
    )
    op.create_index(
        "ix_calibre_web_upload_librislog_book_id",
        "calibre_web_upload",
        ["librislog_book_id"],
        unique=False,
    )
    op.create_index(
        "ix_calibre_web_upload_status",
        "calibre_web_upload",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_calibre_web_upload_calibre_book_id",
        "calibre_web_upload",
        ["calibre_book_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_calibre_web_upload_calibre_book_id", table_name="calibre_web_upload")
    op.drop_index("ix_calibre_web_upload_status", table_name="calibre_web_upload")
    op.drop_index("ix_calibre_web_upload_librislog_book_id", table_name="calibre_web_upload")
    op.drop_index("ix_calibre_web_upload_connection_id", table_name="calibre_web_upload")
    op.drop_table("calibre_web_upload")

    op.drop_index("ix_calibre_web_book_link_calibre_book_id", table_name="calibre_web_book_link")
    op.drop_index("ix_calibre_web_book_link_librislog_book_id", table_name="calibre_web_book_link")
    op.drop_index("ix_calibre_web_book_link_connection_id", table_name="calibre_web_book_link")
    op.drop_table("calibre_web_book_link")

    op.drop_index("ix_calibre_web_connection_user_id", table_name="calibre_web_connection")
    op.drop_table("calibre_web_connection")
