from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.supabase_client import supabase, _ensure_supabase_client as _ensure_db_client
from app.models.schemas import MealEntry, ParseRequest, ParseResponse, WorkoutEntry
from app.services.llm_parser import parse_log_entry
from app.utils.normalization import normalize_exercise_name

router = APIRouter(tags=["parse"])


class ConfirmExerciseRequest(BaseModel):
    exercise_name: str
    muscle_group: str
    sets: int
    reps: int
    weight_lbs: float


class ConfirmWorkoutRequest(BaseModel):
    user_id: str
    exercises: list[ConfirmExerciseRequest] = Field(default_factory=list)
    muscle_groups: list[str] = Field(default_factory=list)
    session_notes: str = ""


def _ensure_supabase_client() -> None:
    global supabase
    if supabase is None:
        supabase = _ensure_db_client()


def _ensure_insert_data(response, table_name: str) -> list[dict]:
    response_error = getattr(response, "error", None)
    if response_error:
        raise RuntimeError(f"Supabase insert error on {table_name}: {response_error}")

    data = getattr(response, "data", None)
    if not data:
        raise RuntimeError(f"Supabase insert returned no data for {table_name}.")

    return data


def _safe_float(value: object) -> float:
    if value is None:
        return 0.0
    return float(value)


def _safe_bool(value: object) -> bool:
    return bool(value) if value is not None else False


def _normalize_groups(groups: list[str] | None) -> list[str]:
    normalized = {str(group).strip().lower() for group in (groups or []) if str(group).strip()}
    return sorted(normalized)


def _detect_template_name(muscle_groups: list[str]) -> str:
    group_set = set(_normalize_groups(muscle_groups))

    push_groups = {"chest", "shoulders", "arms", "triceps"}
    pull_groups = {"back", "arms", "biceps"}
    leg_groups = {"legs", "quads", "hamstrings", "glutes", "calves"}

    if group_set.intersection(push_groups) and "back" not in group_set and "legs" not in group_set:
        return "push"
    if group_set.intersection(pull_groups) and "chest" not in group_set and "legs" not in group_set:
        return "pull"
    if group_set.intersection(leg_groups) and "chest" not in group_set and "back" not in group_set:
        return "legs"
    if {"chest", "back"}.issubset(group_set) and not group_set.intersection(leg_groups):
        return "upper"
    if group_set.intersection(leg_groups) and not group_set.intersection({"chest", "back", "shoulders", "arms"}):
        return "lower"
    if group_set.intersection({"chest", "back"}) and group_set.intersection(leg_groups):
        return "full body"
    if not group_set:
        return "workout"
    return "/".join(sorted(group_set))


def _workout_to_template_exercises(workout: WorkoutEntry) -> list[dict]:
    exercises = []
    for ex in workout.exercises:
        exercises.append(
            {
                "exercise_name": ex.exercise_name,
                "muscle_group": ex.muscle_group,
                "sets": ex.sets,
                "reps": ex.reps,
                "weight_lbs": ex.weight_lbs,
            }
        )
    return exercises


def _build_confirmed_workout_entry(request: ConfirmWorkoutRequest) -> WorkoutEntry:
    exercises = []
    total_volume = 0.0
    muscle_groups_seen = set(_normalize_groups(request.muscle_groups))

    for ex in request.exercises:
        sets = int(ex.sets)
        reps = int(ex.reps)
        weight = float(ex.weight_lbs)
        volume = sets * reps * weight
        estimated_1rm = round(weight * (1 + reps / 30), 1) if weight > 0 else 0.0
        muscle_group = str(ex.muscle_group or "unknown").strip().lower()

        total_volume += volume
        muscle_groups_seen.add(muscle_group)

        exercises.append(
            {
                "exercise_name": normalize_exercise_name(ex.exercise_name),
                "muscle_group": muscle_group,
                "sets": sets,
                "reps": reps,
                "weight_lbs": weight,
                "volume_lbs": round(volume, 1),
                "estimated_1rm": estimated_1rm,
            }
        )

    return WorkoutEntry(
        muscle_groups=sorted(muscle_groups_seen),
        total_volume_lbs=round(total_volume, 1),
        exercises=exercises,
    )


def upsert_workout_template(user_id: str, workout: WorkoutEntry) -> None:
    _ensure_supabase_client()

    normalized_groups = _normalize_groups(workout.muscle_groups)
    template_name = _detect_template_name(normalized_groups)
    exercises_payload = _workout_to_template_exercises(workout)

    try:
        existing_resp = (
            supabase.table("workout_templates")
            .select("id,muscle_groups,usage_count")
            .eq("user_id", user_id)
            .execute()
        )
    except Exception as exc:
        text = str(exc)
        if "workout_templates" in text and "PGRST205" in text:
            print("workout_templates table missing; skipping template upsert until migration is applied.")
            return
        raise

    existing_error = getattr(existing_resp, "error", None)
    if existing_error:
        raise RuntimeError(f"Supabase select error on workout_templates: {existing_error}")

    existing_rows = getattr(existing_resp, "data", None) or []

    matched_row = None
    target_set = set(normalized_groups)
    for row in existing_rows:
        row_set = set(_normalize_groups(row.get("muscle_groups") or []))
        if row_set == target_set:
            matched_row = row
            break

    if matched_row:
        usage_count = int(matched_row.get("usage_count") or 0) + 1
        update_resp = (
            supabase.table("workout_templates")
            .update(
                {
                    "template_name": template_name,
                    "muscle_groups": normalized_groups,
                    "exercises": exercises_payload,
                    "usage_count": usage_count,
                    "last_used": "now()",
                }
            )
            .eq("id", matched_row["id"])
            .execute()
        )
        update_error = getattr(update_resp, "error", None)
        if update_error:
            raise RuntimeError(f"Supabase update error on workout_templates: {update_error}")
        return

    insert_resp = (
        supabase.table("workout_templates")
        .insert(
            {
                "user_id": user_id,
                "template_name": template_name,
                "muscle_groups": normalized_groups,
                "exercises": exercises_payload,
                "usage_count": 1,
                "last_used": "now()",
            }
        )
        .execute()
    )
    _ensure_insert_data(insert_resp, "workout_templates")


def upsert_daily_summary(
    user_id: str,
    entry_type: str,
    meal: MealEntry | None,
    workout: WorkoutEntry | None,
) -> None:
    _ensure_supabase_client()

    today = date.today().isoformat()

    existing_resp = (
        supabase.table("daily_summaries")
        .select("*")
        .eq("user_id", user_id)
        .eq("date", today)
        .limit(1)
        .execute()
    )

    existing_error = getattr(existing_resp, "error", None)
    if existing_error:
        raise RuntimeError(f"Supabase select error on daily_summaries: {existing_error}")

    existing_rows = getattr(existing_resp, "data", None) or []

    meal_delta_cal = meal.total_calories if meal else 0.0
    meal_delta_pro = meal.total_protein_g if meal else 0.0
    meal_delta_carbs = meal.total_carbs_g if meal else 0.0
    meal_delta_fat = meal.total_fat_g if meal else 0.0
    workout_delta_volume = workout.total_volume_lbs if workout else 0.0

    if existing_rows:
        current = existing_rows[0]
        current_id = current.get("id")
        if current_id is None:
            raise RuntimeError("Existing daily_summaries row is missing id.")

        update_payload = {
            "total_calories": _safe_float(current.get("total_calories")) + meal_delta_cal,
            "total_protein_g": _safe_float(current.get("total_protein_g")) + meal_delta_pro,
            "total_carbs_g": _safe_float(current.get("total_carbs_g")) + meal_delta_carbs,
            "total_fat_g": _safe_float(current.get("total_fat_g")) + meal_delta_fat,
            "total_volume_lbs": _safe_float(current.get("total_volume_lbs")) + workout_delta_volume,
            "workout_logged": _safe_bool(current.get("workout_logged")) or (entry_type in ["workout", "both"]),
        }

        update_resp = (
            supabase.table("daily_summaries")
            .update(update_payload)
            .eq("id", current_id)
            .execute()
        )
        update_error = getattr(update_resp, "error", None)
        if update_error:
            raise RuntimeError(f"Supabase update error on daily_summaries: {update_error}")
        return

    insert_payload = {
        "user_id": user_id,
        "date": today,
        "total_calories": meal_delta_cal,
        "total_protein_g": meal_delta_pro,
        "total_carbs_g": meal_delta_carbs,
        "total_fat_g": meal_delta_fat,
        "total_volume_lbs": workout_delta_volume,
        "workout_logged": entry_type in ["workout", "both"],
    }

    insert_resp = supabase.table("daily_summaries").insert(insert_payload).execute()
    _ensure_insert_data(insert_resp, "daily_summaries")


@router.post("/parse", response_model=ParseResponse)
async def parse_entry(request: ParseRequest) -> ParseResponse:
    try:
        _ensure_supabase_client()

        parsed = await parse_log_entry(request.raw_text, user_id=request.user_id)

        log_resp = (
            supabase.table("logs")
            .insert(
                {
                    "user_id": request.user_id,
                    "raw_text": request.raw_text,
                    "entry_type": parsed.entry_type,
                }
            )
            .execute()
        )
        log_data = _ensure_insert_data(log_resp, "logs")
        log_id = log_data[0]["id"]

        if parsed.entry_type in ["meal", "both"]:
            if parsed.meal is None:
                raise RuntimeError("Parsed entry type includes meal but meal payload is missing.")

            meal_resp = (
                supabase.table("meal_entries")
                .insert(
                    {
                        "log_id": log_id,
                        "user_id": request.user_id,
                        "meal_type": parsed.meal.meal_type,
                        "total_calories": parsed.meal.total_calories,
                        "total_protein_g": parsed.meal.total_protein_g,
                        "total_carbs_g": parsed.meal.total_carbs_g,
                        "total_fat_g": parsed.meal.total_fat_g,
                        "confidence": parsed.meal.confidence,
                    }
                )
                .execute()
            )
            meal_data = _ensure_insert_data(meal_resp, "meal_entries")
            meal_entry_id = meal_data[0]["id"]

            for item in parsed.meal.items:
                food_resp = (
                    supabase.table("food_items")
                    .insert(
                        {
                            "meal_entry_id": meal_entry_id,
                            "food_name": item.food_name,
                            "quantity_desc": item.quantity_desc,
                            "calories": item.calories,
                            "protein_g": item.protein_g,
                            "carbs_g": item.carbs_g,
                            "fat_g": item.fat_g,
                            "source": item.source,
                        }
                    )
                    .execute()
                )
                _ensure_insert_data(food_resp, "food_items")

        if parsed.entry_type in ["workout", "both"]:
            if parsed.workout is None:
                raise RuntimeError("Parsed entry type includes workout but workout payload is missing.")

            workout_resp = (
                supabase.table("workout_entries")
                .insert(
                    {
                        "log_id": log_id,
                        "user_id": request.user_id,
                        "muscle_groups": parsed.workout.muscle_groups,
                        "total_volume_lbs": parsed.workout.total_volume_lbs,
                        "session_notes": "",
                    }
                )
                .execute()
            )
            workout_data = _ensure_insert_data(workout_resp, "workout_entries")
            workout_entry_id = workout_data[0]["id"]

            for ex in parsed.workout.exercises:
                exercise_resp = (
                    supabase.table("exercises")
                    .insert(
                        {
                            "workout_entry_id": workout_entry_id,
                            "exercise_name": ex.exercise_name,
                            "muscle_group": ex.muscle_group,
                            "sets": ex.sets,
                            "reps": ex.reps,
                            "weight_lbs": ex.weight_lbs,
                            "volume_lbs": ex.volume_lbs,
                            "estimated_1rm": ex.estimated_1rm,
                        }
                    )
                    .execute()
                )
                _ensure_insert_data(exercise_resp, "exercises")

            upsert_workout_template(request.user_id, parsed.workout)

        upsert_daily_summary(request.user_id, parsed.entry_type, parsed.meal, parsed.workout)

        return parsed
    except HTTPException:
        raise
    except Exception as exc:
        print("Error in POST /api/parse:", exc)
        raise HTTPException(status_code=500, detail=f"Failed to parse and persist entry: {exc}")


@router.post("/workout/confirm")
async def confirm_workout(request: ConfirmWorkoutRequest) -> dict:
    try:
        _ensure_supabase_client()

        if not request.exercises:
            raise HTTPException(status_code=400, detail="exercises cannot be empty")

        workout_entry = _build_confirmed_workout_entry(request)

        raw_text = request.session_notes.strip() or "Confirmed workout entry"
        log_resp = (
            supabase.table("logs")
            .insert(
                {
                    "user_id": request.user_id,
                    "raw_text": raw_text,
                    "entry_type": "workout",
                }
            )
            .execute()
        )
        log_data = _ensure_insert_data(log_resp, "logs")
        log_id = log_data[0]["id"]

        workout_resp = (
            supabase.table("workout_entries")
            .insert(
                {
                    "log_id": log_id,
                    "user_id": request.user_id,
                    "muscle_groups": workout_entry.muscle_groups,
                    "total_volume_lbs": workout_entry.total_volume_lbs,
                    "session_notes": request.session_notes,
                }
            )
            .execute()
        )
        workout_data = _ensure_insert_data(workout_resp, "workout_entries")
        workout_entry_id = workout_data[0]["id"]

        for ex in workout_entry.exercises:
            exercise_resp = (
                supabase.table("exercises")
                .insert(
                    {
                        "workout_entry_id": workout_entry_id,
                        "exercise_name": ex.exercise_name,
                        "muscle_group": ex.muscle_group,
                        "sets": ex.sets,
                        "reps": ex.reps,
                        "weight_lbs": ex.weight_lbs,
                        "volume_lbs": ex.volume_lbs,
                        "estimated_1rm": ex.estimated_1rm,
                    }
                )
                .execute()
            )
            _ensure_insert_data(exercise_resp, "exercises")

        upsert_workout_template(request.user_id, workout_entry)
        upsert_daily_summary(request.user_id, "workout", None, workout_entry)

        return {
            "saved": True,
            "workout_entry_id": workout_entry_id,
            "workout": workout_entry.model_dump(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        print("Error in POST /api/workout/confirm:", exc)
        raise HTTPException(status_code=500, detail=f"Failed to confirm workout: {exc}")
