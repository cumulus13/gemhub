"""Initial schema: cached gem metadata, cached files and daily download counters.

Revision ID: 0001
Revises:
Create Date: 2026-11-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gems",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("latest_version", sa.String(128)),
        sa.Column("summary", sa.Text()),
        sa.Column("downloads", sa.BigInteger(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("versions", sa.JSON(), nullable=False),
        sa.Column("owners", sa.JSON(), nullable=False),
        sa.Column("first_cached_at", sa.DateTime(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_gems_downloads", "gems", ["downloads"])
    op.create_index("ix_gems_first_cached_at", "gems", ["first_cached_at"])

    op.create_table(
        "cached_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.String(300), nullable=False),
        sa.Column("gem_name", sa.String(255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("last_access_at", sa.DateTime(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.UniqueConstraint("filename"),
    )
    op.create_index("ix_cached_files_gem_name", "cached_files", ["gem_name"])
    op.create_index("ix_cached_files_last_access_at", "cached_files", ["last_access_at"])

    op.create_table(
        "daily_downloads",
        sa.Column("gem_name", sa.String(255), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("daily_downloads")
    op.drop_index("ix_cached_files_last_access_at", table_name="cached_files")
    op.drop_index("ix_cached_files_gem_name", table_name="cached_files")
    op.drop_table("cached_files")
    op.drop_index("ix_gems_first_cached_at", table_name="gems")
    op.drop_index("ix_gems_downloads", table_name="gems")
    op.drop_table("gems")
