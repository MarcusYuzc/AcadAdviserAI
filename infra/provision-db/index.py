"""Create one database and login per service on the shared RDS instance.

Each app secret holds username, password and dbname. Runs on every deploy and is idempotent:
it creates what is missing and resets each login's password to the one in its secret.
"""

from __future__ import annotations

import json
import os

import boto3
import psycopg
from psycopg import sql


def _secret(arn: str) -> dict:
    payload = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
    return json.loads(payload)


def _connect(secret: dict, dbname: str):
    return psycopg.connect(
        host=secret["host"],
        port=int(secret.get("port") or 5432),
        user=secret["username"],
        password=secret["password"],
        dbname=dbname,
        sslmode="require",
        autocommit=True,
    )


def _ensure(admin: dict, app: dict) -> None:
    db, user = sql.Identifier(app["dbname"]), sql.Identifier(app["username"])
    with _connect(admin, "postgres") as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (app["dbname"],)).fetchone() is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(db))
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app["username"],)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(sql.SQL(verb + " USER {} WITH PASSWORD {}").format(user, sql.Literal(app["password"])))
        # Each service can reach only its own database.
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(db))
        conn.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(db, user))
    with _connect(admin, app["dbname"]) as conn:
        conn.execute(sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(user))
        conn.execute(sql.SQL("ALTER SCHEMA public OWNER TO {}").format(user))


def handler(event, _context):
    physical_id = event.get("PhysicalResourceId") or "acadadviser-databases"
    if event.get("RequestType") == "Delete":
        # Databases stay with the RDS instance; its own removal policy decides their fate.
        return {"PhysicalResourceId": physical_id}
    admin = _secret(os.environ["ADMIN_SECRET_ARN"])
    for arn in filter(None, os.environ["APP_SECRET_ARNS"].split(",")):
        _ensure(admin, _secret(arn))
    return {"PhysicalResourceId": physical_id}
