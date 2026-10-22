"""Let the service role log in: run after `alembic upgrade head` (compose does it in the `migrate` service).

    DATABASE_URL=<owner url> APP_DB_PASSWORD=<secret> python migrations/service_login.py

Migration 0003 creates `integer_app` without a login, so no password is ever written into a revision.
This sets it from the environment; re-running it is safe and simply rotates the password.
"""

import os

import psycopg
from psycopg import sql

SERVICE_ROLE = "integer_app"
MIN_PASSWORD_LENGTH = 24


def grant_login(database_url: str, role: str, password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"the password must be at least {MIN_PASSWORD_LENGTH} characters long")
    with psycopg.connect(database_url, autocommit=True) as conn:
        # ALTER ROLE takes no bind parameters: the password goes in as a properly quoted literal.
        conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))


if __name__ == "__main__":
    grant_login(os.environ["DATABASE_URL"], SERVICE_ROLE, os.environ["APP_DB_PASSWORD"])
    print(f"{SERVICE_ROLE} can log in")
