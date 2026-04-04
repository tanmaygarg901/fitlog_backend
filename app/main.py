from datetime import date
import os

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.db.supabase_client import supabase, _ensure_supabase_client as _ensure_db_client
from app.routers import dashboard, parse, reports

app = FastAPI(title="FitLog AI API")


def _ensure_supabase_client() -> None:
    global supabase
    if supabase is None:
        supabase = _ensure_db_client()


def _sanitize_env(raw: str) -> str:
    value = str(raw or "").strip()
    if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
        value = value[1:-1].strip()
    return value

# Allow overriding CORS origins via env (comma-separated). Fallback to sensible dev defaults.
# Supports common variable variants to avoid deployment misconfiguration.
_cors_env = _sanitize_env(
    os.getenv("CORS_ORIGINS")
    or os.getenv("CORS_ORIGIN")
    or os.getenv("cors_origins")
    or os.getenv("cors_origin")
    or ""
)
_cors_from_env = [o.strip() for o in _cors_env.split(",") if o.strip()]
_cors_origin_regex = _sanitize_env(
    os.getenv("CORS_ORIGIN_REGEX")
    or os.getenv("CORS_REGEX")
    or os.getenv("cors_origin_regex")
    or os.getenv("cors_regex")
    or ""
)
if "\\\\" in _cors_origin_regex:
    _cors_origin_regex = _cors_origin_regex.replace("\\\\", "\\")
_default_cors_origin_regex = r"^https://([a-z0-9-]+\.)?(lovable\.app|lovableproject\.com)$|^http://localhost:(3000|5173)$"
_explicit_cors_fallback = [
    "https://4460feb8-1185-470f-b647-bd6f9c78ac8f.lovableproject.com",
    "https://id-preview--4460feb8-1185-470f-b647-bd6f9c78ac8f.lovable.app",
    "http://localhost:3000",
    "http://localhost:5173",
]
_cors_allow_origins = list(dict.fromkeys((_cors_from_env or []) + _explicit_cors_fallback))
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins,
    allow_origin_regex=_cors_origin_regex or _default_cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

parse_router = getattr(parse, "router", APIRouter())
reports_router = getattr(reports, "router", APIRouter())
dashboard_router = getattr(dashboard, "router", APIRouter())

app.include_router(parse_router, prefix="/api", tags=["parse"])
app.include_router(reports_router, prefix="/api", tags=["reports"])
app.include_router(dashboard_router, prefix="/api/dashboard", tags=["dashboard"])


@app.get("/", tags=["meta"])
async def root():
    return {"status": "ok", "service": "fitlog-api"}


@app.get("/health", tags=["meta"])
async def health_check():
    return {"status": "ok", "service": "fitlog-api"}


@app.get("/health/db", tags=["meta"])
async def health_db():
    try:
        _ensure_supabase_client()
    except Exception as exc:
        return {"db_ok": False, "error": str(exc)}

    try:
        resp = supabase.table("logs").select("id").limit(1).execute()
        err = getattr(resp, "error", None)
        if err:
            return {"db_ok": False, "error": str(err)}
        return {"db_ok": True}
    except Exception as exc:
        return {"db_ok": False, "error": str(exc)}


@app.get("/api/summary/today", tags=["summary"])
async def get_today_summary(user_id: str = Query(...)):
    try:
        _ensure_supabase_client()
        today = date.today().isoformat()

        response = (
            supabase.table("daily_summaries")
            .select("*")
            .eq("user_id", user_id)
            .eq("date", today)
            .limit(1)
            .execute()
        )
        error = getattr(response, "error", None)
        if error:
            raise RuntimeError(f"Failed fetching today summary: {error}")

        data = getattr(response, "data", None) or []
        if data:
            return data[0]

        return {
            "user_id": user_id,
            "date": today,
            "total_calories": 0,
            "total_protein_g": 0,
            "total_carbs_g": 0,
            "total_fat_g": 0,
            "total_volume_lbs": 0,
            "workout_logged": False,
        }
    except Exception as exc:
        print("Error in GET /api/summary/today:", exc)
        raise HTTPException(status_code=500, detail=f"Failed to fetch today summary: {exc}")


@app.get("/api/history", tags=["history"])
async def get_history(user_id: str = Query(...), limit: int = Query(50, ge=1, le=200)):
    try:
        _ensure_supabase_client()

        logs_resp = (
            supabase.table("logs")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        logs_error = getattr(logs_resp, "error", None)
        if logs_error:
            raise RuntimeError(f"Failed fetching logs: {logs_error}")

        logs = getattr(logs_resp, "data", None) or []
        if not logs:
            return []

        log_ids = [row.get("id") for row in logs if row.get("id") is not None]
        if not log_ids:
            return logs

        meal_resp = (
            supabase.table("meal_entries")
            .select("log_id,meal_type,total_calories,total_protein_g,total_carbs_g,total_fat_g,confidence")
            .in_("log_id", log_ids)
            .execute()
        )
        meal_error = getattr(meal_resp, "error", None)
        if meal_error:
            raise RuntimeError(f"Failed fetching meal_entries: {meal_error}")

        workout_resp = (
            supabase.table("workout_entries")
            .select("log_id,muscle_groups,total_volume_lbs")
            .in_("log_id", log_ids)
            .execute()
        )
        workout_error = getattr(workout_resp, "error", None)
        if workout_error:
            raise RuntimeError(f"Failed fetching workout_entries: {workout_error}")

        meal_by_log = {row["log_id"]: row for row in (getattr(meal_resp, "data", None) or []) if row.get("log_id") is not None}
        workout_by_log = {
            row["log_id"]: row
            for row in (getattr(workout_resp, "data", None) or [])
            if row.get("log_id") is not None
        }

        merged = []
        for row in logs:
            log_id = row.get("id")
            meal = meal_by_log.get(log_id)
            workout = workout_by_log.get(log_id)

            merged.append(
                {
                    **row,
                    "meal_totals": (
                        {
                            "meal_type": meal.get("meal_type"),
                            "total_calories": meal.get("total_calories", 0),
                            "total_protein_g": meal.get("total_protein_g", 0),
                            "total_carbs_g": meal.get("total_carbs_g", 0),
                            "total_fat_g": meal.get("total_fat_g", 0),
                            "confidence": meal.get("confidence"),
                        }
                        if meal
                        else None
                    ),
                    "muscle_groups": workout.get("muscle_groups", []) if workout else [],
                    "total_volume_lbs": workout.get("total_volume_lbs", 0) if workout else 0,
                }
            )

        return merged
    except Exception as exc:
        print("Error in GET /api/history:", exc)
        raise HTTPException(status_code=500, detail=f"Failed to fetch history: {exc}")


@app.on_event("startup")
async def on_startup():
    print("FitLog API started")
