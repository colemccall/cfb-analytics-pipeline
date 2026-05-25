# ARCHIVED: No active callers in local-arch scripts (01–12).
# Scripts use utils/db.get_connection() (psycopg2) for all DB access.
# Kept for reference only — do not import from active scripts.

import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_ANON_KEY"]
        _client = create_client(url, key)
    return _client
