"""Per-client request counters for the rate limit of the public endpoints that write.

One row per client address: the current minute and how many requests it made in it. Shared by all
workers, unlike a counter in process memory.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-22
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE rate_limits (
        client       text PRIMARY KEY,
        window_start timestamptz NOT NULL,
        hits         integer NOT NULL CHECK (hits > 0)
    );
    GRANT SELECT, INSERT, UPDATE ON rate_limits TO integer_app;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE rate_limits;")
