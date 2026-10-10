import os

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://sidekick:sidekick@localhost:5432/sidekick")

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=int(os.getenv("DB_POOL_SIZE", "10")),
                      kwargs={"row_factory": dict_row, "autocommit": True}, open=False)


def open_pool():
    pool.open()
    pool.wait(timeout=60)


def close_pool():
    pool.close()


def all(sql, params=()):
    with pool.connection() as c:
        return c.execute(sql, params).fetchall()


def one(sql, params=()):
    with pool.connection() as c:
        return c.execute(sql, params).fetchone()


def run(sql, params=()):
    with pool.connection() as c:
        c.execute(sql, params)


J = Jsonb  # wrap dicts/lists for JSONB parameters
