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


def _ensure_supabase_client() -> Client:
    if supabase is None:
        raise RuntimeError("Supabase client is not initialized.")
    return supabase


def _normalize_muscle_groups(muscle_groups: list[str] | None) -> set[str]:
    return {str(group).strip().lower() for group in (muscle_groups or []) if str(group).strip()}


def _compose_workout_payload(workout_row: dict, exercise_rows: list[dict], logged_at: str = "") -> dict:
    exercises = []
    for ex in exercise_rows:
        exercises.append(
            {
                "exercise_name": ex.get("exercise_name", "unknown"),
                "muscle_group": str(ex.get("muscle_group", "unknown")).lower(),
                "sets": int(ex.get("sets") or 0),
                "reps": int(ex.get("reps") or 0),
                "weight_lbs": float(ex.get("weight_lbs") or 0),
                "volume_lbs": float(ex.get("volume_lbs") or 0),
                "estimated_1rm": float(ex.get("estimated_1rm") or 0),
            }
        )

    return {
        "logged_at": logged_at,
        "muscle_groups": [str(g).lower() for g in (workout_row.get("muscle_groups") or [])],
        "total_volume_lbs": float(workout_row.get("total_volume_lbs") or 0),
        "exercises": exercises,
    }


async def get_recent_workouts(user_id: str, limit: int = 10) -> list[dict]:
    client = _ensure_supabase_client()

    workout_resp = (
        client.table("workout_entries")
        .select("id,log_id,muscle_groups,total_volume_lbs")
        .eq("user_id", user_id)
        .execute()
    )
    workout_error = getattr(workout_resp, "error", None)
    if workout_error:
        raise RuntimeError(f"Supabase select error on workout_entries: {workout_error}")

    workout_rows = getattr(workout_resp, "data", None) or []
    if not workout_rows:
        return []

    workout_ids = [row.get("id") for row in workout_rows if row.get("id")]
    log_ids = [row.get("log_id") for row in workout_rows if row.get("log_id")]

    exercises_by_workout_id: dict[str, list[dict]] = {}
    if workout_ids:
        exercises_resp = (
            client.table("exercises")
            .select("workout_entry_id,exercise_name,muscle_group,sets,reps,weight_lbs,volume_lbs,estimated_1rm")
            .in_("workout_entry_id", workout_ids)
            .execute()
        )
        exercises_error = getattr(exercises_resp, "error", None)
        if exercises_error:
            raise RuntimeError(f"Supabase select error on exercises: {exercises_error}")

        for ex in getattr(exercises_resp, "data", None) or []:
            key = ex.get("workout_entry_id")
            if not key:
                continue
            exercises_by_workout_id.setdefault(key, []).append(ex)

    log_time_by_id: dict[str, str] = {}
    if log_ids:
        logs_resp = client.table("logs").select("id,created_at").in_("id", log_ids).execute()
        logs_error = getattr(logs_resp, "error", None)
        if logs_error:
            raise RuntimeError(f"Supabase select error on logs: {logs_error}")

        for row in getattr(logs_resp, "data", None) or []:
            if row.get("id"):
                log_time_by_id[row["id"]] = row.get("created_at") or ""

    sorted_workouts = sorted(
        workout_rows,
        key=lambda row: log_time_by_id.get(row.get("log_id"), ""),
        reverse=True,
    )

    payload = []
    for row in sorted_workouts[:limit]:
        payload.append(
            _compose_workout_payload(
                row,
                exercises_by_workout_id.get(row.get("id"), []),
                logged_at=log_time_by_id.get(row.get("log_id"), ""),
            )
        )
    return payload


async def get_last_workout_by_muscle_groups(user_id: str, muscle_groups: list[str]) -> dict | None:
    target = _normalize_muscle_groups(muscle_groups)
    if not target:
        return None

    recent = await get_recent_workouts(user_id=user_id, limit=30)
    best_workout = None
    best_score = -1

    for workout in recent:
        workout_groups = _normalize_muscle_groups(workout.get("muscle_groups") or [])
        overlap = workout_groups.intersection(target)
        if not overlap:
            continue

        extras = len(workout_groups - target)
        missing = len(target - workout_groups)
        score = len(overlap) * 5 - extras * 6 - missing * 2
        if score > best_score:
            best_score = score
            best_workout = workout

    return best_workout


async def get_workout_templates(user_id: str) -> list[dict]:
    client = _ensure_supabase_client()

    response = (
        client.table("workout_templates")
        .select("*")
        .eq("user_id", user_id)
        .order("usage_count", desc=True)
        .execute()
    )
    response_error = getattr(response, "error", None)
    if response_error:
        raise RuntimeError(f"Supabase select error on workout_templates: {response_error}")

    return getattr(response, "data", None) or []
