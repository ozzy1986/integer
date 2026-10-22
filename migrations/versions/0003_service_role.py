"""The service's own role, `integer_app`, with only the privileges it needs.

The service used to connect as the database owner, a superuser in the postgres image. A superuser can
switch the money triggers off (session_replication_role, DISABLE TRIGGER), so they protected against
bugs but not against a compromised service. integer_app cannot: no DDL, no UPDATE/DELETE on the ledger,
UPDATE only on the columns the state machine moves, nothing on the catalog but SELECT.

users.balance is updatable because the ledger trigger runs with the caller's privileges; the guard
trigger still refuses any change that does not come from a ledger row.

Roles are cluster-wide: the role may already exist (another database on the same server), so it is
created only if missing, and downgrade drops it only when no other database still grants it anything.
Logging in is enabled separately by migrations/service_login.py, so no password ever lands in a revision.
A new table needs its GRANTs in the same migration; tests/db/test_service_role.py catches a forgotten one.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-22
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    DO $$ BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'integer_app') THEN
            CREATE ROLE integer_app NOLOGIN;
        END IF;
    END $$;

    GRANT USAGE ON SCHEMA public TO integer_app;
    GRANT SELECT ON models, legs, balance_drift TO integer_app;
    GRANT SELECT, INSERT ON users, tasks, attempts, task_events, payments, ledger TO integer_app;
    GRANT UPDATE (balance) ON users TO integer_app;
    GRANT UPDATE (status, result_url, error, finished_at) ON tasks TO integer_app;
    GRANT UPDATE (status, external_id, error, reason, result_url, lease_until, deadline_at, sent_at, finished_at)
        ON attempts TO integer_app;
    GRANT UPDATE (status, updated_at) ON payments TO integer_app;
    GRANT USAGE ON SEQUENCE ledger_id_seq, task_events_id_seq TO integer_app;
    """)


def downgrade() -> None:
    op.execute("""
    REVOKE ALL ON models, legs, balance_drift, users, tasks, attempts, task_events, payments, ledger
        FROM integer_app;
    REVOKE ALL ON SEQUENCE ledger_id_seq, task_events_id_seq FROM integer_app;
    REVOKE USAGE ON SCHEMA public FROM integer_app;

    DO $$ BEGIN
        IF NOT EXISTS (SELECT FROM pg_shdepend d JOIN pg_roles r ON r.oid = d.refobjid
                        WHERE r.rolname = 'integer_app') THEN
            DROP ROLE integer_app;
        END IF;
    END $$;
    """)
