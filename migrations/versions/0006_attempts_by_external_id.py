"""A webhook may name its attempt by the provider's external_id alone: index that lookup.

Not unique on purpose: a provider that hands out one external_id twice would otherwise fail the recording of
its 202 forever. The lookup treats such an id as unknown instead.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-22
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX attempts_by_external_id ON attempts (provider, external_id) WHERE external_id IS NOT NULL;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX attempts_by_external_id;")
