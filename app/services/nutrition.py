"""Nutrition lookup helpers for FitLog AI."""

from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
from typing import Any

import httpx

from app.models.schemas import FoodItem

USDA_BASE_URL = "https://api.nal.usda.gov/fdc/v1"
USDA_NUTRIENT_IDS = {
    "calories": 1008,
    "protein_g": 1003,
    "carbs_g": 1005,
    "fat_g": 1004,
}
_RESOLVED_GEMINI_CANDIDATES: list[str] | None = None


def _name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


async def lookup_nutrition(food_name: str, quantity_desc: str) -> FoodItem:
    usda_result = await _lookup_usda(food_name, quantity_desc)
    if usda_result is not None:
        return usda_result

    return await _estimate_with_llm(food_name, quantity_desc)


async def _lookup_usda(food_name: str, quantity_desc: str) -> FoodItem | None:
    usda_api_key = os.getenv("USDA_API_KEY")
    if not usda_api_key:
        print("USDA_API_KEY is not set; skipping USDA lookup.")
        return None

    query = food_name.strip() or quantity_desc.strip()
    if not query:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{USDA_BASE_URL}/foods/search",
                params={
                    "query": query,
                    "api_key": usda_api_key,
                    "pageSize": 5,
                    "dataType": "Survey (FNDDS),SR Legacy,Foundation",
                },
            )
            response.raise_for_status()
            data = response.json()

            foods = data.get("foods") or []
            if not foods:
                return None

            food = foods[0]
            food_description = food.get("description", "")
            similarity = _name_similarity(food_name, food_description)

            if similarity < 0.3:
                print(
                    f"USDA match rejected: '{food_name}' vs '{food_description}' "
                    f"(similarity={similarity:.2f})"
                )
                return None

            nutrients = _extract_usda_nutrients(food)
            if len(nutrients) < 4:
                fdc_id = food.get("fdcId")
                if fdc_id is not None:
                    detailed_food = await _fetch_usda_food_details(client, fdc_id, usda_api_key)
                    if detailed_food is not None:
                        nutrients = _extract_usda_nutrients(detailed_food)

            if not nutrients:
                return None

            serving_grams = _parse_serving_grams(quantity_desc, food_name)
            scale = serving_grams / 100.0

            return FoodItem(
                food_name=food_name,
                quantity_desc=quantity_desc,
                calories=round(nutrients.get(USDA_NUTRIENT_IDS["calories"], 0.0) * scale, 1),
                protein_g=round(nutrients.get(USDA_NUTRIENT_IDS["protein_g"], 0.0) * scale, 1),
                carbs_g=round(nutrients.get(USDA_NUTRIENT_IDS["carbs_g"], 0.0) * scale, 1),
                fat_g=round(nutrients.get(USDA_NUTRIENT_IDS["fat_g"], 0.0) * scale, 1),
                source="usda",
            )
    except Exception as exc:
        print(f"USDA lookup failed for '{food_name}': {exc}")
        return None


async def _fetch_usda_food_details(
    client: httpx.AsyncClient,
    fdc_id: Any,
    usda_api_key: str,
) -> dict[str, Any] | None:
    try:
        response = await client.get(
            f"{USDA_BASE_URL}/food/{fdc_id}",
            params={"api_key": usda_api_key},
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _extract_usda_nutrients(food: dict[str, Any]) -> dict[int, float]:
    nutrients: dict[int, float] = {}
    for nutrient in food.get("foodNutrients", []) or []:
        nutrient_id = nutrient.get("nutrientId")
        if nutrient_id is None and isinstance(nutrient.get("nutrient"), dict):
            nutrient_id = nutrient["nutrient"].get("id")

        value = nutrient.get("value")
        if nutrient_id is None or value is None:
            continue

        try:
            nutrients[int(nutrient_id)] = float(value)
        except (TypeError, ValueError):
            continue

    return nutrients


def _parse_serving_grams(quantity_desc: str, food_name: str) -> float:
    quantity_lower = quantity_desc.lower()
    food_lower = food_name.lower()

    gram_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:g|grams?)\b", quantity_lower)
    if gram_match:
        return float(gram_match.group(1))

    kg_match = re.search(r"(\d+(?:\.\d+)?)\s*kg\b", quantity_lower)
    if kg_match:
        return float(kg_match.group(1)) * 1000.0

    count_match = re.search(r"^(\d+(?:\.\d+)?)\s", quantity_lower)
    count = float(count_match.group(1)) if count_match else 1.0

    defaults = {
        "egg": 50,
        "eggs": 50,
        "slice": 30,
        "slices": 30,
        "cup": 240,
        "cups": 240,
        "tablespoon": 15,
        "tbsp": 15,
        "teaspoon": 5,
        "tsp": 5,
        "ounce": 28,
        "oz": 28,
        "pound": 454,
        "lb": 454,
        "scoop": 35,
        "banana": 120,
        "apple": 182,
        "chicken breast": 174,
        "steak": 200,
        "salmon": 170,
        "rice": 158,
        "bread": 30,
        "yogurt": 170,
    }

    for key, grams in defaults.items():
        if key in quantity_lower or key in food_lower:
            return grams * count

    return 100.0 * count


async def _estimate_with_llm(food_name: str, quantity_desc: str) -> FoodItem:
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        print("GEMINI_API_KEY is not set; trying Groq fallback.")
        return await _estimate_with_groq(food_name, quantity_desc)

    prompt = (
        "Estimate the nutritional macros for this food item.\n"
        f"Food: {food_name}\n"
        f"Quantity: {quantity_desc}\n\n"
        "Return ONLY a JSON object with no markdown, no explanation:\n"
        '{"calories": number, "protein_g": number, "carbs_g": number, "fat_g": number}\n\n'
        "Use realistic values based on standard nutritional data."
    )

    gemini_errors = []
    for model_name in _resolve_gemini_model_candidates(gemini_api_key):
        try:
            import google.generativeai as genai

            genai.configure(api_key=gemini_api_key)
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)
            text = (getattr(response, "text", None) or "").strip()
            data = _parse_json_object(text)

            if data is None:
                gemini_errors.append(f"{model_name}: invalid JSON")
                continue

            return FoodItem(
                food_name=food_name,
                quantity_desc=quantity_desc,
                calories=float(data.get("calories", 0) or 0),
                protein_g=float(data.get("protein_g", 0) or 0),
                carbs_g=float(data.get("carbs_g", 0) or 0),
                fat_g=float(data.get("fat_g", 0) or 0),
                source="llm_estimate",
            )
        except Exception as exc:
            gemini_errors.append(f"{model_name}: {exc}")

    if gemini_errors:
        print(f"Gemini estimation failed for '{food_name}' across all models: {' | '.join(gemini_errors)}")

    print(f"Trying Groq fallback for '{food_name}'.")
    return await _estimate_with_groq(food_name, quantity_desc)


def _resolve_gemini_model_candidates(gemini_api_key: str) -> list[str]:
    global _RESOLVED_GEMINI_CANDIDATES
    if _RESOLVED_GEMINI_CANDIDATES is not None:
        return _RESOLVED_GEMINI_CANDIDATES

    env_model = os.getenv("GEMINI_MODEL")
    preferred = [env_model, "gemini-3.1-flash", "gemini-2.5-flash", "gemma-3"]
    deduped_preferred = [name for i, name in enumerate(preferred) if name and name not in preferred[:i]]

    try:
        import google.generativeai as genai

        genai.configure(api_key=gemini_api_key)
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


async def _estimate_with_groq(food_name: str, quantity_desc: str) -> FoodItem:
    groq_api_key = os.getenv("GROQ_API_KEY")
    if not groq_api_key:
        print("GROQ_API_KEY is not set; returning zeroed nutrition estimate.")
        return _failed_food_item(food_name, quantity_desc)

    try:
        from groq import Groq

        client = Groq(api_key=groq_api_key)
        response = client.chat.completions.create(
            model=os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You estimate nutrition macros. "
                        "Return only strict JSON with numeric fields."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Estimate the nutritional macros for this food item.\n"
                        f"Food: {food_name}\n"
                        f"Quantity: {quantity_desc}\n\n"
                        "Return ONLY a JSON object with no markdown, no explanation:\n"
                        '{"calories": number, "protein_g": number, "carbs_g": number, "fat_g": number}'
                    ),
                },
            ],
        )

        text = (response.choices[0].message.content or "").strip()
        data = _parse_json_object(text)
        if data is None:
            return _failed_food_item(food_name, quantity_desc)

        return FoodItem(
            food_name=food_name,
            quantity_desc=quantity_desc,
            calories=float(data.get("calories", 0) or 0),
            protein_g=float(data.get("protein_g", 0) or 0),
            carbs_g=float(data.get("carbs_g", 0) or 0),
            fat_g=float(data.get("fat_g", 0) or 0),
            source="groq_estimate",
        )
    except Exception as exc:
        print(f"Groq estimation failed for '{food_name}': {exc}")
        return _failed_food_item(food_name, quantity_desc)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None

    cleaned = text.strip()
    if "```" in cleaned:
        parts = [part.strip() for part in cleaned.split("```") if part.strip()]
        for part in parts:
            if part.lower().startswith("json"):
                cleaned = part[4:].strip()
                break
        else:
            cleaned = parts[-1]

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        parsed = json.loads(cleaned[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _failed_food_item(food_name: str, quantity_desc: str) -> FoodItem:
    return FoodItem(
        food_name=food_name,
        quantity_desc=quantity_desc,
        calories=0,
        protein_g=0,
        carbs_g=0,
        fat_g=0,
        source="failed",
    )


