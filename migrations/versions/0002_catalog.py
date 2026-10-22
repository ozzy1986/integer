"""Starting catalog: models and their legs (provider chains) in priority order.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    INSERT INTO models (slug, price) VALUES ('image-fast', 2), ('video-long', 25);
    INSERT INTO legs (model_slug, priority, provider, max_ref_images, skip_on_moderation) VALUES
        ('image-fast', 1, 'alpha', 4,  false),
        ('image-fast', 2, 'beta',  10, false),
        ('video-long', 1, 'beta',  2,  false),
        ('video-long', 2, 'alpha', 1,  true);
    """)


def downgrade() -> None:
    op.execute("DELETE FROM models WHERE slug IN ('image-fast', 'video-long');")
