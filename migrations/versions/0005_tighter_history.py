"""Two more invariants the code already keeps, now kept by the database too.

* An attempt has an error type exactly when it failed. A late result accepted after the watchdog (rule 6)
  used to keep `technical` on a succeeded attempt; `reason` still says the watchdog gave up first, and the
  event log keeps the failure.
* A ledger row about a task belongs to that task's user: a charge or refund cannot land on someone else.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-22
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    UPDATE attempts SET error = NULL WHERE status <> 'failed' AND error IS NOT NULL;
    ALTER TABLE attempts ADD CONSTRAINT attempts_error_iff_failed CHECK ((status = 'failed') = (error IS NOT NULL));

    ALTER TABLE tasks ADD CONSTRAINT tasks_id_user_id UNIQUE (id, user_id);
    ALTER TABLE ledger ADD CONSTRAINT ledger_task_of_the_user
        FOREIGN KEY (task_id, user_id) REFERENCES tasks (id, user_id);
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE ledger DROP CONSTRAINT ledger_task_of_the_user;
    ALTER TABLE tasks DROP CONSTRAINT tasks_id_user_id;
    ALTER TABLE attempts DROP CONSTRAINT attempts_error_iff_failed;
    """)
