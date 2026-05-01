import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

_working_url: str | None = None


def _get_working_url() -> str:
    global _working_url
    if _working_url:
        return _working_url
    candidates = [
        os.environ.get("DATABASE_URL", ""),
        os.environ.get("DATABASE_URL_POOLER", ""),
    ]
    for url in candidates:
        if not url:
            continue
        try:
            conn = psycopg2.connect(url, connect_timeout=5)
            conn.close()
            _working_url = url
            print(f"  [db] Connected via {'pooler' if 'pooler' in url else 'direct'}")
            return _working_url
        except Exception:
            continue
    raise RuntimeError("Could not connect to Supabase via direct or pooler URL. Check DATABASE_URL and DATABASE_URL_POOLER in .env")


@contextmanager
def get_connection():
    conn = psycopg2.connect(_get_working_url())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def bulk_upsert(table: str, rows: list[dict], conflict_col: str | list[str]) -> int:
    """Insert rows, updating on conflict. Returns number of rows affected."""
    if not rows:
        return 0

    cols = list(rows[0].keys())
    conflict_cols = [conflict_col] if isinstance(conflict_col, str) else conflict_col
    update_cols = [c for c in cols if c not in conflict_cols]

    col_str = ", ".join(cols)
    placeholder = "(" + ", ".join(f"%({c})s" for c in cols) + ")"
    conflict_str = ", ".join(conflict_cols)

    if update_cols:
        update_str = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        on_conflict = f"ON CONFLICT ({conflict_str}) DO UPDATE SET {update_str}"
    else:
        on_conflict = f"ON CONFLICT ({conflict_str}) DO NOTHING"

    sql = f"INSERT INTO {table} ({col_str}) VALUES %s {on_conflict}"

    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur, sql, rows, template=placeholder, page_size=500
            )
            return cur.rowcount
