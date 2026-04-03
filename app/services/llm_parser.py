import json
import os
import re
import time

from app.models.schemas import (
    ExerciseItem,
    FoodItem,
    MealEntry,
    ParseResponse,
    WorkoutEntry,
)
from app.db.supabase_client import get_last_workout_by_muscle_groups
from app.services.nutrition import lookup_nutrition
from app.utils.normalization import normalize_exercise_name

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
_RESOLVED_GEMINI_CANDIDATES: list[str] | None = None


def _resolve_gemini_model_candidates() -> list[str]:
    import google.generativeai as genai

    global _RESOLVED_GEMINI_CANDIDATES
    if _RESOLVED_GEMINI_CANDIDATES is not None:
        return _RESOLVED_GEMINI_CANDIDATES

    env_model = os.getenv("GEMINI_MODEL")
    preferred = [
        env_model,
        "gemini-3.1-flash",
        "gemini-2.5-flash",
        "gemma-3",
    ]

    deduped_preferred: list[str] = []
    for model_name in preferred:
        if model_name and model_name not in deduped_preferred:
            deduped_preferred.append(model_name)

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        available = set()
        for listed in genai.list_models():
            model_name = getattr(listed, "name", "")
            methods = set(getattr(listed, "supported_generation_methods", []) or [])
            if "generateContent" in methods:
                available.add(model_name)
                if model_name.startswith("models/"):
                    available.add(model_name.split("models/", 1)[1])

        resolved = [m for m in deduped_preferred if m in available or f"models/{m}" in available]
        if resolved:
            _RESOLVED_GEMINI_CANDIDATES = resolved
            return _RESOLVED_GEMINI_CANDIDATES
    except Exception:
        pass

    _RESOLVED_GEMINI_CANDIDATES = deduped_preferred
    return _RESOLVED_GEMINI_CANDIDATES


def _is_rate_limited_error(error: Exception) -> bool:
    error_str = str(error).lower()
    return "429" in error_str or "quota" in error_str or "rate" in error_str


def _get_gemini_model(model_name: str):
    import google.generativeai as genai

    genai.configure(api_key=GEMINI_API_KEY)
    return genai.GenerativeModel(model_name)


def _get_groq_client():
    from groq import Groq

    return Groq(api_key=GROQ_API_KEY)


def _get_groq_model_name() -> str:
    return os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")


def _call_llm(prompt: str, max_retries: int = 2) -> str:
    gemini_errors = []

    for model_name in _resolve_gemini_model_candidates():
        for attempt in range(max_retries + 1):
            try:
                model = _get_gemini_model(model_name)
                response = model.generate_content(prompt)
                return response.text.strip()
            except Exception as e:
                if _is_rate_limited_error(e) and attempt < max_retries:
                    wait = 2**attempt
                    print(f"Gemini ({model_name}) quota hit, waiting {wait}s before retry {attempt + 1}")
                    time.sleep(wait)
                    continue

                gemini_errors.append(f"{model_name} attempt {attempt + 1}: {e}")
                break

    print("All Gemini candidates failed. Falling back to Groq.")
    try:
        client = _get_groq_client()
        response = client.chat.completions.create(
            model=_get_groq_model_name(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return response.choices[0].message.content.strip()
    except Exception as e2:
        gemini_error_text = " | ".join(gemini_errors) if gemini_errors else "No Gemini attempts recorded"
        raise RuntimeError(f"All LLMs failed. Gemini: {gemini_error_text}. Groq: {e2}")


def _clean_json(text: str) -> str:
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 3:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
    return text.strip()


def _parse_json_with_retry(prompt: str, context: str = "") -> dict:
    response_text = _call_llm(prompt)

    try:
        return json.loads(_clean_json(response_text))
    except json.JSONDecodeError:
        print("JSON parse failed on first attempt. Retrying with correction.")
        correction_prompt = f"""You returned invalid JSON. Fix it.

Your previous response was:
{response_text}

Return ONLY valid JSON with no markdown, no explanation, no code fences.
{context}"""
        retry_text = _call_llm(correction_prompt)
        try:
            return json.loads(_clean_json(retry_text))
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM returned invalid JSON after retry: {e}\nResponse: {retry_text}")


def _classify_entry(raw_text: str) -> str:
    prompt = f"""You are a health log classifier.

Classify this log entry as exactly one of: meal, workout, both

Rules:
- 'meal' if it only describes food/drinks consumed
- 'workout' if it only describes exercise/physical activity
- 'both' if it describes both food and exercise

Log entry: "{raw_text}"

Return ONLY one word: meal, workout, or both"""

    result = _call_llm(prompt).lower().strip()

    if result not in ["meal", "workout", "both"]:
        text_lower = raw_text.lower()
        has_food = any(
            w in text_lower
            for w in [
                "ate",
                "had",
                "drank",
                "breakfast",
                "lunch",
                "dinner",
                "snack",
                "protein",
                "calories",
                "cup",
                "grams",
            ]
        )
        has_workout = any(
            w in text_lower
            for w in [
                "sets",
                "reps",
                "press",
                "squat",
                "deadlift",
                "ran",
                "lifted",
                "gym",
                "workout",
                "exercise",
                "miles",
                "minutes",
            ]
        )
        if has_food and has_workout:
            return "both"
        if has_workout:
            return "workout"
        return "meal"

    return result


async def _parse_meal(raw_text: str) -> MealEntry:
    prompt = f"""Extract all food items from this meal log entry.

Log entry: "{raw_text}"

Return ONLY a JSON object with this exact structure:
{{
  "meal_type": "breakfast" | "lunch" | "dinner" | "snack" | "unknown",
  "items": [
    {{
      "food_name": "name of the food item",
      "quantity_desc": "quantity as described or estimated (e.g. '1 cup', '150g', '2 eggs', '1 large piece')"
    }}
  ]
}}

Rules:
- Split combination dishes into components when possible
- If quantity is not mentioned, estimate a typical single serving
- meal_type should be inferred from context (time of day words, food types)
- Include drinks if mentioned"""

    data = _parse_json_with_retry(
        prompt,
        context='Required format: {"meal_type": string, "items": [{"food_name": string, "quantity_desc": string}]}',
    )

    food_items: list[FoodItem] = []
    total_calories = 0.0
    total_protein = 0.0
    total_carbs = 0.0
    total_fat = 0.0

    sources_used = set()

    for item in data.get("items", []):
        food_item = await lookup_nutrition(
            item.get("food_name", "unknown food"),
            item.get("quantity_desc", "1 serving"),
        )
        food_items.append(food_item)
        total_calories += food_item.calories
        total_protein += food_item.protein_g
        total_carbs += food_item.carbs_g
        total_fat += food_item.fat_g
        sources_used.add(food_item.source)

    confidence = "verified" if sources_used == {"usda"} else "estimated"

    return MealEntry(
        meal_type=data.get("meal_type", "unknown"),
        total_calories=round(total_calories, 1),
        total_protein_g=round(total_protein, 1),
        total_carbs_g=round(total_carbs, 1),
        total_fat_g=round(total_fat, 1),
        confidence=confidence,
        items=food_items,
    )


WORKOUT_GROUP_KEYWORDS = {
    "chest": ["chest", "pec", "pecs"],
    "back": ["back", "lats", "lat"],
    "legs": ["legs", "leg", "quads", "quad", "hamstrings", "hamstring", "glutes", "calves", "calf"],
    "shoulders": ["shoulder", "shoulders", "delts", "delt"],
    "arms": ["arms", "arm", "biceps", "bicep", "triceps", "tricep", "bis", "tris"],
    "core": ["core", "abs", "ab"],
}

EXERCISE_HINTS = [
    "bench",
    "press",
    "squat",
    "deadlift",
    "row",
    "pulldown",
    "curl",
    "extension",
    "fly",
    "lunge",
    "raise",
    "pushdown",
]


def _detect_muscle_groups_from_text(raw_text: str) -> list[str]:
    text = raw_text.lower()
    groups = set()

    if "push" in text:
        groups.update(["chest", "shoulders", "arms"])
    if "pull" in text:
        groups.update(["back", "arms"])

    for group, keywords in WORKOUT_GROUP_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            groups.add(group)

    return list(groups)


def _is_shorthand_workout_log(raw_text: str, detected_groups: list[str]) -> bool:
    text = raw_text.lower().strip()
    words = [w for w in re.split(r"\s+", text) if w]

    has_group = bool(detected_groups)
    has_shorthand_phrase = any(
        phrase in text
        for phrase in [
            "usual",
            "normal",
            "same",
            "as usual",
            "same as",
            "day",
            "workout",
            "hit",
            "did",
        ]
    )
    has_set_rep_pattern = bool(re.search(r"\d+\s*[x×]\s*\d+", text))

    return has_group and (has_shorthand_phrase or len(words) <= 12) and not has_set_rep_pattern


def _build_workout_prompt(raw_text: str) -> str:
    return f"""Extract all exercises from this workout log entry.

Log entry: "{raw_text}"

Return ONLY a JSON object with this exact structure:
{{
  "exercises": [
    {{
      "exercise_name": "full exercise name",
      "muscle_group": "primary muscle group",
      "sets": number,
      "reps": number,
      "weight_lbs": number
    }}
  ]
}}

Rules for muscle_group — use ONLY these values:
chest, back, legs, shoulders, arms, core, cardio

Rules for weight_lbs:
- Convert to lbs if kg mentioned (multiply by 2.205)
- Use 0 for bodyweight exercises (pull-ups, push-ups, dips)
- Use 0 for cardio (running, cycling)

Rules for reps:
- If "to failure" or "AMRAP", estimate 10
- If a range like "8-12", use the lower number
- For cardio, use 1

Rules for sets:
- If not mentioned, default to 3
- "4x8" means 4 sets of 8 reps"""


def _template_to_exercise_seed(template_workout: dict) -> list[dict]:
    seeded = []
    for ex in template_workout.get("exercises", []):
        seeded.append(
            {
                "exercise_name": ex.get("exercise_name", "unknown"),
                "muscle_group": ex.get("muscle_group", "unknown"),
                "sets": int(ex.get("sets") or 3),
                "reps": int(ex.get("reps") or 10),
                "weight_lbs": float(ex.get("weight_lbs") or 0),
            }
        )
    return seeded


def _detect_template_modifications(raw_text: str) -> list[str]:
    text = raw_text.lower().strip()
    modifications: list[str] = []

    for match in re.finditer(
        r"(?:went up|up|increased?|\+)\s*(\d+(?:\.\d+)?)\s*(?:lb|lbs|pounds?)?\s*(?:on|for)?\s*([a-z][a-z\s-]+)",
        text,
    ):
        modifications.append(f"increase {match.group(2).strip()} weight by {match.group(1)} lbs")

    for match in re.finditer(r"new\s+pr\s+on\s+([a-z][a-z\s-]+).*?(\d+(?:\.\d+)?)", text):
        modifications.append(f"set {match.group(1).strip()} weight to {match.group(2)} lbs")

    for match in re.finditer(r"dropped?\s+the\s+weight.*?(?:did|to)\s*(\d+(?:\.\d+)?)\s*(?:lb|lbs|pounds?)?", text):
        modifications.append(f"set target exercise weight to {match.group(1)} lbs")

    for match in re.finditer(r"went\s+down\s+(\d+(?:\.\d+)?)\s*(?:lb|lbs|pounds?)?\s+on\s+everything", text):
        modifications.append(f"decrease all exercise weights by {match.group(1)} lbs")

    for match in re.finditer(r"only\s+did\s+(\d+)\s+sets?\s+of\s+([a-z][a-z\s-]+)", text):
        modifications.append(f"set {match.group(2).strip()} sets to {match.group(1)}")

    for match in re.finditer(r"added\s+an\s+extra\s+set\s+of\s+([a-z][a-z\s-]+)", text):
        modifications.append(f"increase {match.group(1).strip()} sets by 1")

    for match in re.finditer(r"(\d+)(?:s)?\s+across\s+the\s+board", text):
        modifications.append(f"set all exercise reps to {match.group(1)}")

    for match in re.finditer(r"swapped\s+([a-z][a-z\s-]+)\s+for\s+([a-z][a-z\s-]+)", text):
        modifications.append(f"replace {match.group(1).strip()} with {match.group(2).strip()}")

    for match in re.finditer(r"skipped\s+([a-z][a-z\s-]+)", text):
        modifications.append(f"remove {match.group(1).strip()} from workout")

    for match in re.finditer(r"added\s+([a-z][a-z\s-]+)\s+at\s+the\s+end", text):
        modifications.append(f"append {match.group(1).strip()} with defaults if missing")

    # Replace entire routine with a listed set of exercises
    full_replace_patterns = [
        re.compile(r"(?:switched|changed|updated)\s+(?:my\s+)?(?:push|pull|legs|workout)(?:\s+day)?\s+(?:to|with)\s+(?:these\s+)?new\s+exercises[:\-]\s*(.+)", re.IGNORECASE),
        re.compile(r"replace(?:d)?\s+(?:my\s+)?(?:push|pull|legs|workout)(?:\s+day)?\s+(?:with|by)\s+(.+)", re.IGNORECASE),
    ]
    for pat in full_replace_patterns:
        m = pat.search(raw_text)
        if m:
            listed = m.group(1).strip()
            if listed:
                modifications.append(f"replace all exercises with: {listed}")

    if "only had" in text and "main lifts" in text:
        modifications.append("keep only first 2-3 main lifts")

    just_match = re.search(r"just\s+([a-z][a-z\s,\-and]+)", text)
    if just_match:
        modifications.append(f"keep only exercises matching: {just_match.group(1).strip()}")

    if "same as last time" in text or "same as usual" in text or "same workout" in text:
        modifications.append("no structural changes; use template as-is unless explicit edits are present")

    if not modifications:
        modifications.append("no explicit modifications detected")

    return modifications


def _build_template_modification_prompt(raw_text: str, template_workout: dict, detected_modifications: list[str]) -> str:
    template_json = json.dumps(template_workout, ensure_ascii=True)
    detected_json = json.dumps(detected_modifications, ensure_ascii=True)

    return f"""You are modifying a workout template based on a shorthand user note.

Detected modifications: {detected_json}
Applying to template: {template_json}

User shorthand entry: "{raw_text}"

Apply modifications carefully:
- Weight changes (increase/set/decrease all)
- Set/rep changes (single exercise or across all)
- Exercise substitutions (swap/remove/add)
- Partial sessions (only main lifts or specific exercises)
- If no meaningful changes are present, keep template as-is

For added exercises with no details, use sensible defaults:
- sets=3, reps=15, weight_lbs=50 unless context strongly suggests otherwise.

Return ONLY valid JSON with exactly this structure:
{{
  "detected_modifications": ["..."],
  "applied_modifications": ["..."],
  "exercises": [
    {{
      "exercise_name": "full exercise name",
      "muscle_group": "primary muscle group",
      "sets": number,
      "reps": number,
      "weight_lbs": number
    }}
  ]
}}

Rules for muscle_group — use ONLY these values:
chest, back, legs, shoulders, arms, core, cardio"""


def _apply_shorthand_template_shortcuts(raw_text: str, template_workout: dict) -> dict | None:
    text = raw_text.lower()
    template_exercises = _template_to_exercise_seed(template_workout)
    if not template_exercises:
        return None

    no_change_phrases = [
        "same as last time",
        "same workout",
        "same as usual",
        "as usual",
        "no changes",
        "usual workout",
    ]
    if any(phrase in text for phrase in no_change_phrases):
        return {"exercises": template_exercises}

    delta_patterns = [
        re.compile(r"(?:went up|up|increased?)\s+(\d+(?:\.\d+)?)\s*(?:lb|lbs|pounds?)\s+(?:on|for)\s+([a-z][a-z\s-]+)", re.IGNORECASE),
        re.compile(r"\+(\d+(?:\.\d+)?)\s*(?:lb|lbs|pounds?)\s+(?:on|for)\s+([a-z][a-z\s-]+)", re.IGNORECASE),
    ]

    modifications = []
    for pattern in delta_patterns:
        for match in pattern.finditer(raw_text):
            delta = float(match.group(1))
            target = match.group(2).strip().lower()
            modifications.append((delta, target))

    if not modifications:
        return None

    updated = False
    for delta, target in modifications:
        for ex in template_exercises:
            name = str(ex.get("exercise_name", "")).lower()
            if target in name or name in target:
                ex["weight_lbs"] = round(float(ex.get("weight_lbs") or 0) + delta, 1)
                updated = True
                break

    if updated:
        return {"exercises": template_exercises}

    return None


async def _parse_workout(raw_text: str, user_id: str = None) -> WorkoutEntry:
    prompt = _build_workout_prompt(raw_text)
    data = None

    if user_id:
        detected_groups = _detect_muscle_groups_from_text(raw_text)
        if _is_shorthand_workout_log(raw_text, detected_groups):
            try:
                template_workout = await get_last_workout_by_muscle_groups(user_id, detected_groups)
            except Exception as template_exc:
                print("Workout template lookup failed, continuing with normal extraction:", template_exc)
                template_workout = None

            if template_workout:
                detected_modifications = _detect_template_modifications(raw_text)
                prompt = _build_template_modification_prompt(raw_text, template_workout, detected_modifications)

                try:
                    template_data = _parse_json_with_retry(
                        prompt,
                        context='Required: {"detected_modifications": [str], "applied_modifications": [str], "exercises": [{"exercise_name": str, "muscle_group": str, "sets": int, "reps": int, "weight_lbs": float}]}',
                    )
                    template_exercises = template_data.get("exercises", [])
                    data = {"exercises": template_exercises}

                    applied_modifications = template_data.get("applied_modifications", detected_modifications)
                    template_json = json.dumps(template_workout, ensure_ascii=True)
                    result_json = json.dumps(template_exercises, ensure_ascii=True)
                    print(
                        f"Detected modifications: {applied_modifications}. "
                        f"Applying to template: {template_json}. "
                        f"Result: {result_json}"
                    )
                except Exception as mod_exc:
                    print("Template modification prompt failed, trying deterministic shortcut path:", mod_exc)
                    shortcut_data = _apply_shorthand_template_shortcuts(raw_text, template_workout)
                    if shortcut_data is not None:
                        data = shortcut_data
                        template_json = json.dumps(template_workout, ensure_ascii=True)
                        result_json = json.dumps(shortcut_data.get("exercises", []), ensure_ascii=True)
                        print(
                            f"Detected modifications: {detected_modifications}. "
                            f"Applying to template: {template_json}. "
                            f"Result: {result_json}"
                        )

    if data is None:
        data = _parse_json_with_retry(
            prompt,
            context='Required: {"exercises": [{"exercise_name": str, "muscle_group": str, "sets": int, "reps": int, "weight_lbs": float}]}',
        )

    exercises = []
    total_volume = 0.0
    muscle_groups_seen = set()

    for ex in data.get("exercises", []):
        sets = int(ex.get("sets", 3))
        reps = int(ex.get("reps", 10))
        weight = float(ex.get("weight_lbs", 0))

        volume = sets * reps * weight
        total_volume += volume

        estimated_1rm = round(weight * (1 + reps / 30), 1) if weight > 0 else 0.0

        muscle_group = ex.get("muscle_group", "unknown").lower()
        muscle_groups_seen.add(muscle_group)

        exercises.append(
            ExerciseItem(
                exercise_name=normalize_exercise_name(ex.get("exercise_name", "unknown")),
                muscle_group=muscle_group,
                sets=sets,
                reps=reps,
                weight_lbs=weight,
                volume_lbs=round(volume, 1),
                estimated_1rm=estimated_1rm,
            )
        )

    if not exercises:
        print("LLM returned no exercises, trying regex fallback")
        fallback_data = _regex_workout_fallback(raw_text)
        for ex in fallback_data:
            sets = int(ex.get("sets", 3))
            reps = int(ex.get("reps", 10))
            weight = float(ex.get("weight_lbs", 0))

            volume = sets * reps * weight
            total_volume += volume

            estimated_1rm = round(weight * (1 + reps / 30), 1) if weight > 0 else 0.0

            muscle_group = ex.get("muscle_group", "unknown").lower()
            muscle_groups_seen.add(muscle_group)

            exercises.append(
                ExerciseItem(
                    exercise_name=normalize_exercise_name(ex.get("exercise_name", "unknown")),
                    muscle_group=muscle_group,
                    sets=sets,
                    reps=reps,
                    weight_lbs=weight,
                    volume_lbs=round(volume, 1),
                    estimated_1rm=estimated_1rm,
                )
            )

    return WorkoutEntry(
        muscle_groups=list(muscle_groups_seen),
        total_volume_lbs=round(total_volume, 1),
        exercises=exercises,
    )


def _regex_workout_fallback(raw_text: str) -> list[dict]:
    exercises = []
    text = raw_text.lower()

    set_rep_pattern = re.compile(
        r"([a-z\s]+?)\s+"
        r"(\d+(?:\.\d+)?)\s*(?:lbs?|kg)?\s+"
        r"(\d+)\s*[x×]\s*(\d+)",
        re.IGNORECASE,
    )
    for match in set_rep_pattern.finditer(text):
        name = match.group(1).strip()
        weight = float(match.group(2)) if match.group(2) else 0.0
        sets = int(match.group(3))
        reps = int(match.group(4))
        exercises.append(
            {
                "exercise_name": name,
                "muscle_group": _infer_muscle_group(name),
                "sets": sets,
                "reps": reps,
                "weight_lbs": weight,
            }
        )

    cardio_miles = re.search(r"ran\s+(\d+(?:\.\d+)?)\s+miles?", text)
    cardio_mins = re.search(r"ran\s+(?:for\s+)?(\d+)\s+min", text)
    if cardio_miles or cardio_mins:
        miles = float(cardio_miles.group(1)) if cardio_miles else 0
        mins = int(cardio_mins.group(1)) if cardio_mins else 0
        exercises.append(
            {
                "exercise_name": f"running {miles} miles" if miles else f"running {mins} min",
                "muscle_group": "cardio",
                "sets": 1,
                "reps": 1,
                "weight_lbs": 0.0,
            }
        )

    return exercises


def _infer_muscle_group(exercise_name: str) -> str:
    name = exercise_name.lower()
    mapping = {
        "chest": ["bench", "chest", "fly", "pec", "push-up", "pushup"],
        "back": ["row", "pull", "lat", "deadlift", "pulldown"],
        "legs": ["squat", "leg", "lunge", "quad", "hamstring", "calf", "rdl"],
        "shoulders": ["shoulder", "press", "ohp", "lateral", "delt", "military"],
        "arms": ["curl", "tricep", "bicep", "hammer", "pushdown", "extension"],
        "core": ["plank", "crunch", "ab", "core", "sit-up", "situp"],
        "cardio": ["run", "ran", "bike", "cycle", "swim", "elliptical", "treadmill"],
    }
    for group, keywords in mapping.items():
        if any(kw in name for kw in keywords):
            return group
    return "unknown"


async def parse_log_entry(raw_text: str, user_id: str = None) -> ParseResponse:
    entry_type = _classify_entry(raw_text)
    print(f"Classified as: {entry_type}")

    meal_entry = None
    workout_entry = None

    if entry_type in ["meal", "both"]:
        meal_entry = await _parse_meal(raw_text)
        print(f"Meal parsed: {meal_entry.total_calories} cal, {meal_entry.total_protein_g}g protein")

    if entry_type in ["workout", "both"]:
        workout_entry = await _parse_workout(raw_text, user_id=user_id)
        print(
            f"Workout parsed: {len(workout_entry.exercises)} exercises, "
            f"{workout_entry.total_volume_lbs} lbs total volume"
        )

    return ParseResponse(
        entry_type=entry_type,
        meal=meal_entry,
        workout=workout_entry,
        raw_text=raw_text,
    )
