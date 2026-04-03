from datetime import date, datetime, time, timedelta, timezone
import math

from fastapi import APIRouter, HTTPException, Query

from app.db.supabase_client import supabase
from app.services.correlation import compute_weekly_correlations
from app.utils.dates import zero_fill_days
from app.utils.normalization import normalize_exercise_name

router = APIRouter(tags=["dashboard"])

DEFAULT_GOALS = {
    "calories": 2500,
    "protein_g": 180,
    "carbs_g": 250,
    "fat_g": 70,
}


def _ensure_supabase_client() -> None:
    if supabase is None:
        raise RuntimeError("Supabase client is not initialized.")


def _date_from_timestamp(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).date().isoformat()
    except Exception:
        return ""


def _window_start_iso(days: int) -> str:
    start_day = date.today() - timedelta(days=days - 1)
    start_dt = datetime.combine(start_day, time.min, tzinfo=timezone.utc)
    return start_dt.isoformat()


@router.get("/correlations")
async def get_dashboard_correlations(user_id: str = Query(...)) -> dict:
    try:
        result = compute_weekly_correlations(user_id)
        pairs = result.get("pairs", []) or []

        table_pairs = []
        for item in pairs:
            pair_name = str(item.get("pair") or "")
            variable_pair = pair_name.replace("_vs_", " vs ").replace("_", " ").title()
            coefficient = float(item.get("coefficient") or 0)
            table_pairs.append(
                {
                    "pair": pair_name,
                    "variable_pair": variable_pair,
                    "direction": item.get("direction", "None"),
                    "coefficient": coefficient,
                    "strength": coefficient,
                }
            )

        return {
            "enough_data": result.get("enough_data", False),
            "message": result.get("message", ""),
            "days": int(result.get("days", 0) or 0),
            "pairs": table_pairs,
            "stats": result.get("stats", {}),
        }
    except Exception as exc:
        print("Error in GET /api/dashboard/correlations:", exc)
        raise HTTPException(status_code=500, detail=f"Failed to fetch dashboard correlations: {exc}")


@router.get("/weekly-macros")
async def get_weekly_macros(user_id: str = Query(...)) -> dict:
    try:
        _ensure_supabase_client()

        start_day = (date.today() - timedelta(days=6)).isoformat()
        end_day = date.today().isoformat()

        summaries_resp = (
            supabase.table("daily_summaries")
            .select("date,total_calories,total_protein_g,total_carbs_g,total_fat_g")
            .eq("user_id", user_id)
            .gte("date", start_day)
            .lte("date", end_day)
            .execute()
        )
        summaries_error = getattr(summaries_resp, "error", None)
        if summaries_error:
            raise RuntimeError(f"Failed fetching daily summaries: {summaries_error}")

        macro_rows = []
        for row in getattr(summaries_resp, "data", None) or []:
            macro_rows.append(
                {
                    "date": str(row.get("date", "")),
                    "calories": float(row.get("total_calories") or 0),
                    "protein_g": float(row.get("total_protein_g") or 0),
                    "carbs_g": float(row.get("total_carbs_g") or 0),
                    "fat_g": float(row.get("total_fat_g") or 0),
                }
            )

        days = zero_fill_days(macro_rows, days=7, date_key="date")
        for row in days:
            row["calories"] = float(row.get("calories", 0))
            row["protein_g"] = float(row.get("protein_g", 0))
            row["carbs_g"] = float(row.get("carbs_g", 0))
            row["fat_g"] = float(row.get("fat_g", 0))

        goals_rows = []
        try:
            goals_resp = (
                supabase.table("user_goals")
                .select("calories,protein_g,carbs_g,fat_g")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            goals_error = getattr(goals_resp, "error", None)
            if goals_error:
                goals_text = str(goals_error)
                if "user_goals" in goals_text and "PGRST205" in goals_text:
                    goals_rows = []
                else:
                    raise RuntimeError(f"Failed fetching user goals: {goals_error}")
            else:
                goals_rows = getattr(goals_resp, "data", None) or []
        except Exception as goals_exc:
            goals_text = str(goals_exc)
            if "user_goals" in goals_text and "PGRST205" in goals_text:
                goals_rows = []
            else:
                raise

        if goals_rows:
            goals_row = goals_rows[0]
            targets = {
                "calories": int(goals_row.get("calories") or DEFAULT_GOALS["calories"]),
                "protein_g": int(goals_row.get("protein_g") or DEFAULT_GOALS["protein_g"]),
                "carbs_g": int(goals_row.get("carbs_g") or DEFAULT_GOALS["carbs_g"]),
                "fat_g": int(goals_row.get("fat_g") or DEFAULT_GOALS["fat_g"]),
            }
        else:
            targets = dict(DEFAULT_GOALS)

        return {"days": days, "targets": targets}
    except HTTPException:
        raise
    except Exception as exc:
        print("Error in GET /api/dashboard/weekly-macros:", exc)
        raise HTTPException(status_code=500, detail=f"Failed to fetch weekly macros: {exc}")


@router.get("/workout-volume")
async def get_workout_volume(user_id: str = Query(...)) -> dict:
    try:
        _ensure_supabase_client()

        logs_resp = (
            supabase.table("logs")
            .select("id,created_at")
            .eq("user_id", user_id)
            .gte("created_at", _window_start_iso(14))
            .execute()
        )
        logs_error = getattr(logs_resp, "error", None)
        if logs_error:
            raise RuntimeError(f"Failed fetching logs: {logs_error}")

        logs = getattr(logs_resp, "data", None) or []
        log_date_by_id = {
            row["id"]: _date_from_timestamp(row.get("created_at"))
            for row in logs
            if row.get("id") and _date_from_timestamp(row.get("created_at"))
        }
        log_ids = list(log_date_by_id.keys())

        workouts = []
        exercise_rows = []
        if log_ids:
            workout_resp = (
                supabase.table("workout_entries")
                .select("id,log_id")
                .eq("user_id", user_id)
                .in_("log_id", log_ids)
                .execute()
            )
            workout_error = getattr(workout_resp, "error", None)
            if workout_error:
                raise RuntimeError(f"Failed fetching workout entries: {workout_error}")

            workouts = getattr(workout_resp, "data", None) or []
            workout_ids = [row.get("id") for row in workouts if row.get("id")]

            if workout_ids:
                exercises_resp = (
                    supabase.table("exercises")
                    .select("workout_entry_id,sets,reps,volume_lbs")
                    .in_("workout_entry_id", workout_ids)
                    .execute()
                )
                exercises_error = getattr(exercises_resp, "error", None)
                if exercises_error:
                    raise RuntimeError(f"Failed fetching exercises: {exercises_error}")
                exercise_rows = getattr(exercises_resp, "data", None) or []

        by_workout: dict[str, dict] = {}
        for ex in exercise_rows:
            workout_id = ex.get("workout_entry_id")
            if not workout_id:
                continue
            agg = by_workout.setdefault(workout_id, {"total_volume_lbs": 0.0, "sets": 0, "reps": 0})
            agg["total_volume_lbs"] += float(ex.get("volume_lbs") or 0)
            agg["sets"] += int(ex.get("sets") or 0)
            agg["reps"] += int(ex.get("reps") or 0)

        by_date: dict[str, dict] = {}
        for workout in workouts:
            workout_id = workout.get("id")
            log_id = workout.get("log_id")
            day = log_date_by_id.get(log_id)
            if not workout_id or not day:
                continue

            date_agg = by_date.setdefault(
                day,
                {"date": day, "total_volume_lbs": 0.0, "sets": 0, "reps": 0, "sessions": 0},
            )
            workout_agg = by_workout.get(workout_id, {"total_volume_lbs": 0.0, "sets": 0, "reps": 0})
            date_agg["total_volume_lbs"] += float(workout_agg.get("total_volume_lbs") or 0)
            date_agg["sets"] += int(workout_agg.get("sets") or 0)
            date_agg["reps"] += int(workout_agg.get("reps") or 0)
            date_agg["sessions"] += 1

        rows = list(by_date.values())
        days = zero_fill_days(rows, days=14, date_key="date")
        for row in days:
            row["total_volume_lbs"] = float(row.get("total_volume_lbs", 0))
            row["sets"] = int(row.get("sets", 0))
            row["reps"] = int(row.get("reps", 0))
            row["sessions"] = int(row.get("sessions", 0))

        return {"days": days}
    except HTTPException:
        raise
    except Exception as exc:
        print("Error in GET /api/dashboard/workout-volume:", exc)
        raise HTTPException(status_code=500, detail=f"Failed to fetch workout volume: {exc}")


@router.get("/muscle-distribution")
async def get_muscle_distribution(user_id: str = Query(...), window: int = Query(7)) -> dict:
    try:
        _ensure_supabase_client()

        if window not in {7, 14, 28}:
            raise HTTPException(status_code=400, detail="window must be one of 7, 14, 28")

        logs_resp = (
            supabase.table("logs")
            .select("id")
            .eq("user_id", user_id)
            .gte("created_at", _window_start_iso(window))
            .execute()
        )
        logs_error = getattr(logs_resp, "error", None)
        if logs_error:
            raise RuntimeError(f"Failed fetching logs: {logs_error}")

        log_ids = [row.get("id") for row in (getattr(logs_resp, "data", None) or []) if row.get("id")]
        if not log_ids:
            return {"window_days": window, "by_group": []}

        workouts_resp = (
            supabase.table("workout_entries")
            .select("id,log_id")
            .eq("user_id", user_id)
            .in_("log_id", log_ids)
            .execute()
        )
        workouts_error = getattr(workouts_resp, "error", None)
        if workouts_error:
            raise RuntimeError(f"Failed fetching workout entries: {workouts_error}")

        workout_ids = [row.get("id") for row in (getattr(workouts_resp, "data", None) or []) if row.get("id")]
        if not workout_ids:
            return {"window_days": window, "by_group": []}

        exercises_resp = (
            supabase.table("exercises")
            .select("muscle_group,volume_lbs")
            .in_("workout_entry_id", workout_ids)
            .execute()
        )
        exercises_error = getattr(exercises_resp, "error", None)
        if exercises_error:
            raise RuntimeError(f"Failed fetching exercises: {exercises_error}")

        volume_by_group: dict[str, float] = {}
        for ex in getattr(exercises_resp, "data", None) or []:
            muscle_group = str(ex.get("muscle_group") or "unknown").lower().strip()
            volume_by_group[muscle_group] = volume_by_group.get(muscle_group, 0.0) + float(ex.get("volume_lbs") or 0)

        total_volume = sum(volume_by_group.values())
        by_group = []
        for muscle_group, volume in volume_by_group.items():
            percent = 0.0 if total_volume <= 0 else round((volume / total_volume) * 100, 1)
            by_group.append(
                {
                    "muscle_group": muscle_group,
                    "volume_lbs": round(volume, 1),
                    "percent": percent,
                }
            )

        by_group.sort(key=lambda row: row["volume_lbs"], reverse=True)
        return {"window_days": window, "by_group": by_group}
    except HTTPException:
        raise
    except Exception as exc:
        print("Error in GET /api/dashboard/muscle-distribution:", exc)
        raise HTTPException(status_code=500, detail=f"Failed to fetch muscle distribution: {exc}")


@router.get("/summary")
async def get_summary(user_id: str = Query(...)) -> dict:
    try:
        _ensure_supabase_client()

        today = date.today()
        today_iso = today.isoformat()

        today_summary_resp = (
            supabase.table("daily_summaries")
            .select("total_calories,total_protein_g")
            .eq("user_id", user_id)
            .eq("date", today_iso)
            .limit(1)
            .execute()
        )
        today_error = getattr(today_summary_resp, "error", None)
        if today_error:
            raise RuntimeError(f"Failed fetching today's summary: {today_error}")

        today_rows = getattr(today_summary_resp, "data", None) or []
        today_row = today_rows[0] if today_rows else {}

        week_start = today - timedelta(days=today.weekday())
        next_week_start = week_start + timedelta(days=7)

        week_logs_resp = (
            supabase.table("logs")
            .select("id")
            .eq("user_id", user_id)
            .gte("created_at", datetime.combine(week_start, time.min, tzinfo=timezone.utc).isoformat())
            .lt("created_at", datetime.combine(next_week_start, time.min, tzinfo=timezone.utc).isoformat())
            .execute()
        )
        week_logs_error = getattr(week_logs_resp, "error", None)
        if week_logs_error:
            raise RuntimeError(f"Failed fetching weekly logs: {week_logs_error}")

        week_log_ids = [row.get("id") for row in (getattr(week_logs_resp, "data", None) or []) if row.get("id")]

        workouts_this_week = 0
        if week_log_ids:
            workouts_resp = (
                supabase.table("workout_entries")
                .select("id")
                .eq("user_id", user_id)
                .in_("log_id", week_log_ids)
                .execute()
            )
            workouts_error = getattr(workouts_resp, "error", None)
            if workouts_error:
                raise RuntimeError(f"Failed fetching weekly workouts: {workouts_error}")
            workouts_this_week = len(getattr(workouts_resp, "data", None) or [])

        streak_logs_resp = (
            supabase.table("logs")
            .select("created_at")
            .eq("user_id", user_id)
            .gte("created_at", _window_start_iso(365))
            .execute()
        )
        streak_logs_error = getattr(streak_logs_resp, "error", None)
        if streak_logs_error:
            raise RuntimeError(f"Failed fetching logs for streak: {streak_logs_error}")

        logged_days = {
            _date_from_timestamp(row.get("created_at"))
            for row in (getattr(streak_logs_resp, "data", None) or [])
            if _date_from_timestamp(row.get("created_at"))
        }

        streak = 0
        cursor = today
        while cursor.isoformat() in logged_days:
            streak += 1
            cursor -= timedelta(days=1)

        return {
            "calories": max(0, int(round(float(today_row.get("total_calories") or 0)))),
            "protein_g": max(0, int(round(float(today_row.get("total_protein_g") or 0)))),
            "workouts_this_week": max(0, int(workouts_this_week)),
            "logging_streak_days": max(0, int(streak)),
        }
    except HTTPException:
        raise
    except Exception as exc:
        print("Error in GET /api/dashboard/summary:", exc)
        raise HTTPException(status_code=500, detail=f"Failed to fetch summary: {exc}")


@router.get("/prs")
async def get_prs(user_id: str = Query(...)) -> dict:
    try:
        _ensure_supabase_client()

        workout_resp = (
            supabase.table("workout_entries")
            .select("id,log_id")
            .eq("user_id", user_id)
            .execute()
        )
        workout_error = getattr(workout_resp, "error", None)
        if workout_error:
            raise RuntimeError(f"Failed fetching workout entries: {workout_error}")

        workouts = getattr(workout_resp, "data", None) or []
        workout_ids = [row.get("id") for row in workouts if row.get("id")]
        if not workout_ids:
            return {"prs": []}

        workout_to_log = {row["id"]: row.get("log_id") for row in workouts if row.get("id")}
        log_ids = [log_id for log_id in workout_to_log.values() if log_id]

        log_date_by_id: dict[str, str] = {}
        if log_ids:
            logs_resp = supabase.table("logs").select("id,created_at").in_("id", log_ids).execute()
            logs_error = getattr(logs_resp, "error", None)
            if logs_error:
                raise RuntimeError(f"Failed fetching logs for PR timeline: {logs_error}")
            for row in getattr(logs_resp, "data", None) or []:
                if row.get("id"):
                    log_date_by_id[row["id"]] = _date_from_timestamp(row.get("created_at"))

        exercises_resp = (
            supabase.table("exercises")
            .select("workout_entry_id,exercise_name,weight_lbs,estimated_1rm,reps")
            .in_("workout_entry_id", workout_ids)
            .execute()
        )
        exercises_error = getattr(exercises_resp, "error", None)
        if exercises_error:
            raise RuntimeError(f"Failed fetching exercises for PRs: {exercises_error}")

        rows_by_name: dict[str, list[dict]] = {}
        for row in getattr(exercises_resp, "data", None) or []:
            workout_entry_id = row.get("workout_entry_id")
            if not workout_entry_id:
                continue

            normalized_name = normalize_exercise_name(str(row.get("exercise_name") or ""))
            weight = float(row.get("weight_lbs") or 0)
            estimated_1rm = float(row.get("estimated_1rm") or 0)
            reps = int(row.get("reps") or 0)
            date_iso = log_date_by_id.get(workout_to_log.get(workout_entry_id), "")

            rows_by_name.setdefault(normalized_name, []).append(
                {
                    "weight_lbs": weight,
                    "estimated_1rm": estimated_1rm,
                    "reps": reps,
                    "date": date_iso,
                }
            )

        prs = []
        for exercise_name, items in rows_by_name.items():
            if not items:
                continue

            ranked = sorted(items, key=lambda r: (r["weight_lbs"], r["date"]), reverse=True)
            current = ranked[0]
            latest_date = max((i.get("date") or "" for i in items), default="")

            distinct_weights = sorted({float(item["weight_lbs"]) for item in ranked if float(item.get("weight_lbs") or 0) > 0}, reverse=True)
            previous_best = distinct_weights[1] if len(distinct_weights) > 1 else None
            delta_lbs = None if previous_best is None else round(current["weight_lbs"] - previous_best, 1)
            if current.get("date") != latest_date:
                delta_lbs = None

            # 1RM fallback if missing: Epley using reps of the selected top set (default 1 if unknown)
            current_est_1rm = float(current.get("estimated_1rm") or 0)
            if current_est_1rm <= 0 and current.get("weight_lbs", 0) > 0:
                reps = int(current.get("reps") or 1)
                current_est_1rm = round(float(current["weight_lbs"]) * (1 + reps / 30), 1)

            prs.append(
                {
                    "exercise_name": normalize_exercise_name(exercise_name),
                    "best_weight_lbs": round(current["weight_lbs"], 1),
                    "best_weight": round(current["weight_lbs"], 1),
                    "best_1rm_est": round(float(current_est_1rm or 0), 1),
                    "est_1rm": round(float(current_est_1rm or 0), 1),
                    "one_rm": round(float(current_est_1rm or 0), 1),
                    "one_rep_max": round(float(current_est_1rm or 0), 1),
                    "estimated_one_rm": round(float(current_est_1rm or 0), 1),
                    "date": current["date"],
                    "date_achieved": current["date"],
                    "delta_lbs": delta_lbs,
                    "delta": delta_lbs,
                }
            )

        prs.sort(key=lambda r: (r.get("date") or "", r["exercise_name"]), reverse=True)

        for pr in prs:
            value = pr.get("delta_lbs")
            if value is not None and isinstance(value, float) and math.isnan(value):
                pr["delta_lbs"] = None

        return {"prs": prs}
    except HTTPException:
        raise
    except Exception as exc:
        print("Error in GET /api/dashboard/prs:", exc)
        raise HTTPException(status_code=500, detail=f"Failed to fetch PRs: {exc}")
