"""Schema: users, catalog, tasks with attempts, payments and the append-only ledger.

Money invariants live here, in the database, so no code path can bypass them:
* users.balance changes only through a ledger row (guard trigger + ledger triggers);
* the ledger is append-only;
* one credit per payment, one charge and at most one refund per task (partial unique indexes);
* a refund must equal the task's charge; a balance never goes negative.

Revision ID: 0001
Revises:
Create Date: 2026-10-22
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TYPE task_status    AS ENUM ('in_progress', 'succeeded', 'failed');
    CREATE TYPE attempt_status AS ENUM ('pending', 'sent', 'succeeded', 'failed', 'superseded');
    CREATE TYPE provider_error AS ENUM ('technical', 'moderation', 'payload');
    CREATE TYPE ledger_kind    AS ENUM ('payment', 'charge', 'refund');

    -- Users are not registered: the id comes from the caller (a Telegram user id) and the row
    -- is created on first use. balance is a cached sum of the ledger, guarded by triggers below.
    CREATE TABLE users (
        id         bigint PRIMARY KEY CHECK (id > 0),
        balance    bigint NOT NULL DEFAULT 0 CHECK (balance >= 0),
        created_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE models (
        slug  text PRIMARY KEY CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
        price integer NOT NULL CHECK (price > 0)
    );

    CREATE TABLE legs (
        model_slug         text NOT NULL REFERENCES models ON DELETE CASCADE,
        priority           smallint NOT NULL CHECK (priority > 0),
        provider           text NOT NULL,
        max_ref_images     smallint NOT NULL CHECK (max_ref_images >= 0),
        skip_on_moderation boolean NOT NULL DEFAULT false,
        PRIMARY KEY (model_slug, priority)
    );

    CREATE TABLE tasks (
        id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id         bigint NOT NULL REFERENCES users,
        model_slug      text NOT NULL REFERENCES models,
        prompt          text NOT NULL,
        ref_images      text[] NOT NULL DEFAULT '{}',
        price           integer NOT NULL CHECK (price > 0),
        status          task_status NOT NULL DEFAULT 'in_progress',
        result_url      text,
        error           provider_error,
        idempotency_key text,
        request_hash    text,
        created_at      timestamptz NOT NULL DEFAULT now(),
        finished_at     timestamptz,
        CHECK ((status = 'succeeded') = (result_url IS NOT NULL)),
        CHECK ((status = 'in_progress') = (finished_at IS NULL)),
        CHECK ((idempotency_key IS NULL) = (request_hash IS NULL))
    );
    CREATE UNIQUE INDEX tasks_idempotency ON tasks (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
    CREATE INDEX tasks_by_user ON tasks (user_id, created_at DESC);
    CREATE INDEX tasks_active ON tasks (user_id) WHERE status = 'in_progress';

    -- One row per try of a leg: this is the task's history.
    CREATE TABLE attempts (
        id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id      uuid NOT NULL REFERENCES tasks ON DELETE CASCADE,
        number       smallint NOT NULL CHECK (number > 0),
        leg_priority smallint NOT NULL,
        provider     text NOT NULL,
        status       attempt_status NOT NULL DEFAULT 'pending',
        external_id  text,
        error        provider_error,
        reason       text,              -- what exactly happened: webhook, http_503, send_timeout, watchdog ...
        result_url   text,
        lease_until  timestamptz,       -- dispatcher claim; an expired lease means the sender crashed
        deadline_at  timestamptz,       -- watchdog fires after this
        created_at   timestamptz NOT NULL DEFAULT now(),
        sent_at      timestamptz,
        finished_at  timestamptz,
        UNIQUE (task_id, number)
    );
    CREATE UNIQUE INDEX attempts_one_live ON attempts (task_id) WHERE status IN ('pending', 'sent');
    CREATE INDEX attempts_to_dispatch ON attempts (created_at) WHERE status = 'pending';
    CREATE INDEX attempts_overdue ON attempts (deadline_at) WHERE status = 'sent';

    -- Everything that happened to a task, including ignored duplicates: for debugging and the UI.
    CREATE TABLE task_events (
        id         bigserial PRIMARY KEY,
        task_id    uuid NOT NULL REFERENCES tasks ON DELETE CASCADE,
        attempt_id uuid REFERENCES attempts ON DELETE CASCADE,
        kind       text NOT NULL,
        detail     jsonb NOT NULL DEFAULT '{}',
        created_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE INDEX task_events_by_task ON task_events (task_id, id);

    CREATE TABLE payments (
        id         text PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
        user_id    bigint NOT NULL REFERENCES users,
        tokens     bigint NOT NULL CHECK (tokens > 0),
        status     text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE ledger (
        id            bigserial PRIMARY KEY,
        user_id       bigint NOT NULL REFERENCES users,
        kind          ledger_kind NOT NULL,
        amount        bigint NOT NULL,
        balance_after bigint NOT NULL CHECK (balance_after >= 0),
        task_id       uuid REFERENCES tasks,
        payment_id    text REFERENCES payments,
        created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT ledger_reference CHECK (CASE kind
            WHEN 'payment' THEN payment_id IS NOT NULL AND task_id IS NULL AND amount > 0
            WHEN 'charge'  THEN task_id IS NOT NULL AND payment_id IS NULL AND amount < 0
            WHEN 'refund'  THEN task_id IS NOT NULL AND payment_id IS NULL AND amount > 0
        END)
    );
    CREATE UNIQUE INDEX ledger_one_credit_per_payment ON ledger (payment_id) WHERE kind = 'payment';
    CREATE UNIQUE INDEX ledger_one_charge_per_task    ON ledger (task_id)    WHERE kind = 'charge';
    CREATE UNIQUE INDEX ledger_one_refund_per_task    ON ledger (task_id)    WHERE kind = 'refund';
    CREATE INDEX ledger_by_user ON ledger (user_id, id DESC);

    -- BEFORE: lock the user, validate, compute balance_after. Nothing is written yet, so a row
    -- skipped by ON CONFLICT DO NOTHING leaves no trace.
    CREATE FUNCTION ledger_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        current_balance bigint;
        charged bigint;
    BEGIN
        SELECT balance INTO current_balance FROM users WHERE id = NEW.user_id FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'ledger: unknown user %', NEW.user_id USING ERRCODE = 'foreign_key_violation';
        END IF;
        IF NEW.kind = 'refund' THEN
            SELECT -amount INTO charged FROM ledger
             WHERE task_id = NEW.task_id AND kind = 'charge' AND user_id = NEW.user_id;
            IF charged IS DISTINCT FROM NEW.amount THEN
                RAISE EXCEPTION 'ledger: refund % does not match the charge of task %', NEW.amount, NEW.task_id
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        NEW.balance_after := current_balance + NEW.amount;
        NEW.created_at := clock_timestamp();
        RETURN NEW;
    END $$;

    -- AFTER: fires only for rows that were really inserted. If the balance moved since BEFORE
    -- (several rows for one user in a single statement), refuse instead of writing a wrong number.
    CREATE FUNCTION ledger_after_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        new_balance bigint;
    BEGIN
        UPDATE users SET balance = balance + NEW.amount WHERE id = NEW.user_id RETURNING balance INTO new_balance;
        IF new_balance IS DISTINCT FROM NEW.balance_after THEN
            RAISE EXCEPTION 'ledger: insert one row per statement for user %', NEW.user_id;
        END IF;
        RETURN NULL;
    END $$;

    CREATE FUNCTION ledger_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'ledger is append-only: % is not allowed', TG_OP;
    END $$;

    CREATE FUNCTION users_balance_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF (TG_OP = 'INSERT' AND NEW.balance <> 0)
           OR (TG_OP = 'UPDATE' AND NEW.balance IS DISTINCT FROM OLD.balance AND pg_trigger_depth() < 2) THEN
            RAISE EXCEPTION 'users.balance changes only through the ledger';
        END IF;
        RETURN NEW;
    END $$;

    CREATE TRIGGER ledger_before_insert BEFORE INSERT ON ledger
        FOR EACH ROW EXECUTE FUNCTION ledger_before_insert();
    CREATE TRIGGER ledger_after_insert AFTER INSERT ON ledger
        FOR EACH ROW EXECUTE FUNCTION ledger_after_insert();
    CREATE TRIGGER ledger_no_update_delete BEFORE UPDATE OR DELETE ON ledger
        FOR EACH ROW EXECUTE FUNCTION ledger_append_only();
    CREATE TRIGGER ledger_no_truncate BEFORE TRUNCATE ON ledger
        FOR EACH STATEMENT EXECUTE FUNCTION ledger_append_only();
    CREATE TRIGGER users_balance_guard BEFORE INSERT OR UPDATE ON users
        FOR EACH ROW EXECUTE FUNCTION users_balance_guard();

    -- Must always be empty; checked by tests and by GET /health/invariants.
    CREATE VIEW balance_drift AS
        SELECT u.id AS user_id, u.balance, COALESCE(sum(l.amount), 0) AS ledger_sum
          FROM users u LEFT JOIN ledger l ON l.user_id = u.id
         GROUP BY u.id
        HAVING u.balance <> COALESCE(sum(l.amount), 0);
    """)


def downgrade() -> None:
    op.execute("""
    DROP VIEW balance_drift;
    DROP TABLE ledger, payments, task_events, attempts, tasks, legs, models, users;
    DROP FUNCTION ledger_before_insert, ledger_after_insert, ledger_append_only, users_balance_guard;
    DROP TYPE ledger_kind, provider_error, attempt_status, task_status;
    """)
