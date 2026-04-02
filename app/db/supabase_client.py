import os
from typing import Optional

from supabase import Client, create_client

supabase: Optional[Client] = None

try:
    supabase_url = os.environ["SUPABASE_URL"]
    supabase_service_role_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    supabase = create_client(supabase_url, supabase_service_role_key)
except KeyError as missing:
    print(
        "[Supabase] Missing required environment variable:",
        str(missing),
    )
except Exception as exc:  # pragma: no cover - defensive logging
    print("[Supabase] Failed to initialize client:", exc)
