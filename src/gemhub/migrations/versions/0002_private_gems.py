"""Private gems: API keys, owned gem names, pushed releases, and a flag on gems.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("scopes", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime()),
        sa.Column("revoked_at", sa.DateTime()),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_table(
        "private_gems",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("owner_key_id", sa.Integer(), sa.ForeignKey("api_keys.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "private_releases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "gem_id",
            sa.Integer(),
            sa.ForeignKey("private_gems.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("gem_name", sa.String(255), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(300), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("yanked", sa.Boolean(), nullable=False),
        sa.Column("downloads", sa.Integer(), nullable=False),
        sa.Column("pushed_at", sa.DateTime(), nullable=False),
        sa.Column(
            "pushed_by_key_id", sa.Integer(), sa.ForeignKey("api_keys.id", ondelete="SET NULL")
        ),
        sa.UniqueConstraint("filename"),
        sa.UniqueConstraint("gem_id", "version", "platform", name="uq_release"),
    )
    op.create_index("ix_private_releases_gem_id", "private_releases", ["gem_id"])
    op.create_index("ix_private_releases_gem_name", "private_releases", ["gem_name"])

    with op.batch_alter_table("gems") as batch:
        batch.add_column(
            sa.Column("is_private", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("gems") as batch:
        batch.drop_column("is_private")
    op.drop_index("ix_private_releases_gem_name", table_name="private_releases")
    op.drop_index("ix_private_releases_gem_id", table_name="private_releases")
    op.drop_table("private_releases")
    op.drop_table("private_gems")
    op.drop_table("api_keys")
