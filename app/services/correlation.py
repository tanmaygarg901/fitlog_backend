from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

import pandas as pd

from app.db.supabase_client import supabase


def _ensure_supabase_client() -> None:
    if supabase is None:
        raise RuntimeError("Supabase client is not initialized.")


def _direction_label(r: float) -> str:
    if r > 0.5:
        return "Strong Positive"
    if 0.2 <= r <= 0.5:
        return "Moderate Positive"
    if -0.2 <= r <= 0.2:
        return "Neutral"
    if -0.5 <= r < -0.2:
        return "Moderate Negative"
    return "Strong Negative"


def _get_gemini_candidates() -> list[str]:
    env_model = os.getenv("GEMINI_MODEL")
    preferred = [env_model, "gemini-3.1-flash", "gemini-2.5-flash", "gemma-3"]
    deduped = [name for i, name in enumerate(preferred) if name and name not in preferred[:i]]
    return deduped


def _fetch_daily_summaries(user_id: str) -> list[dict[str, Any]]:
    _ensure_supabase_client()

    start_date = (date.today() - timedelta(days=27)).isoformat()
    response = (
        supabase.table("daily_summaries")
        .select("*")
        .eq("user_id", user_id)
        .gte("date", start_date)
        .order("date", desc=False)
        .execute()
    )

    error = getattr(response, "error", None)
    if error:
        raise RuntimeError(f"Failed querying daily_summaries: {error}")

    return getattr(response, "data", None) or []


def compute_weekly_correlations(user_id: str) -> dict[str, Any]:
    rows = _fetch_daily_summaries(user_id)
    if len(rows) < 5:
        return {
            "enough_data": False,
            "message": "Not enough data yet — keep logging for at least 5 days to unlock your first correlation report.",
            "pairs": [],
            "stats": {},
            "days": len(rows),
        }

    df = pd.DataFrame(rows)
    if df.empty:
        return {
            "enough_data": False,
            "message": "Not enough data yet — keep logging for at least 5 days to unlock your first correlation report.",
            "pairs": [],
            "stats": {},
            "days": 0,
        }

    column_aliases = {
        "total_calories": ["total_calories"],
        "total_protein_g": ["total_protein_g", "total_protein"],
        "total_carbs_g": ["total_carbs_g", "total_carbs"],
        "total_fat_g": ["total_fat_g", "total_fat"],
        "total_workout_volume": ["total_workout_volume", "total_volume_lbs"],
    }

    normalized = pd.DataFrame(index=df.index)
    for target_col, candidates in column_aliases.items():
        source = next((c for c in candidates if c in df.columns), None)
        if source is not None:
            normalized[target_col] = pd.to_numeric(df[source], errors="coerce")

    usable_cols = [
        col
        for col in normalized.columns
        if normalized[col].notna().sum() >= 2 and normalized[col].nunique(dropna=True) > 1
    ]

    corr_matrix = normalized[usable_cols].corr() if usable_cols else pd.DataFrame()

    pair_specs = [
        ("total_protein_g", "total_workout_volume", "protein_vs_workout_volume"),
        ("total_calories", "total_workout_volume", "calories_vs_workout_volume"),
        ("total_carbs_g", "total_workout_volume", "carbs_vs_workout_volume"),
        ("total_fat_g", "total_workout_volume", "fat_vs_workout_volume"),
    ]

    pairs = []
    for left, right, label in pair_specs:
        if left in corr_matrix.index and right in corr_matrix.columns:
            r_value = corr_matrix.loc[left, right]
            if pd.notna(r_value):
                r = round(float(r_value), 2)
                pairs.append(
                    {
                        "pair": label,
                        "coefficient": r,
                        "direction": _direction_label(r),
                    }
                )

    avg_calories = round(float(normalized["total_calories"].dropna().mean()), 1) if "total_calories" in normalized else 0.0
    avg_protein = round(float(normalized["total_protein_g"].dropna().mean()), 1) if "total_protein_g" in normalized else 0.0

    workout_series = (
        normalized["total_workout_volume"].fillna(0.0)
        if "total_workout_volume" in normalized
        else pd.Series([0.0] * len(normalized), index=normalized.index)
    )
    total_workout_sessions = int((workout_series > 0).sum())

    best_workout_volume = 0.0
    best_workout_day = "N/A"
    if "total_workout_volume" in normalized and not workout_series.empty:
        idx = workout_series.idxmax()
        best_workout_volume = round(float(workout_series.loc[idx]), 1)
        if best_workout_volume > 0 and "date" in df.columns:
            best_workout_day = str(df.loc[idx, "date"])

    stats = {
        "avg_daily_calories": avg_calories,
        "avg_daily_protein_g": avg_protein,
        "total_workout_sessions": total_workout_sessions,
        "best_workout_volume_day": best_workout_day,
        "best_workout_volume": best_workout_volume,
        "days_analyzed": int(len(df)),
    }

    return {
        "enough_data": True,
        "message": "ok",
        "pairs": pairs,
        "stats": stats,
        "days": int(len(df)),
    }


def _call_report_llm(prompt: str) -> str:
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    gemini_errors = []

    if gemini_api_key:
        try:
            import google.generativeai as genai

            genai.configure(api_key=gemini_api_key)
            for model_name in _get_gemini_candidates():
                try:
                    model = genai.GenerativeModel(model_name)
                    response = model.generate_content(prompt)
                    text = (getattr(response, "text", None) or "").strip()
                    if text:
                        return text
                    gemini_errors.append(f"{model_name}: empty response")
                except Exception as exc:
                    gemini_errors.append(f"{model_name}: {exc}")
        except Exception as exc:
            gemini_errors.append(f"Gemini setup failed: {exc}")

    groq_api_key = os.getenv("GROQ_API_KEY")
    if groq_api_key:
        try:
            from groq import Groq

            client = Groq(api_key=groq_api_key)
            response = client.chat.completions.create(
                model=os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
            text = (response.choices[0].message.content or "").strip()
            if text:
                return text
        except Exception as exc:
            gemini_errors.append(f"Groq failed: {exc}")

    raise RuntimeError(f"Failed to generate report text: {' | '.join(gemini_errors)}")


def generate_weekly_report(user_id: str) -> str:
    analysis = compute_weekly_correlations(user_id)
    if not analysis.get("enough_data"):
        return "Not enough data yet — keep logging for at least 5 days to unlock your first correlation report."

    stats = analysis["stats"]
    pairs = analysis["pairs"]

    pair_lines = []
    for pair in pairs:
        pair_lines.append(f"- {pair['pair']}: r={pair['coefficient']} ({pair['direction']})")
    if not pair_lines:
        pair_lines.append("- No valid workout-related correlations available yet.")
    pair_lines_text = "\n".join(pair_lines)

    prompt = (
        "You are a personal health coach analyzing a user's 4-week nutrition and training data. "
        "Write a friendly, specific, actionable weekly insight report in exactly 3 short paragraphs. "
        "Cite the actual numbers. End with exactly 2 bullet point recommendations for next week. "
        "Be encouraging but honest.\n\n"
        f"Days analyzed: {analysis['days']}\n"
        f"Average daily calories: {stats['avg_daily_calories']}\n"
        f"Average daily protein (g): {stats['avg_daily_protein_g']}\n"
        f"Total workout sessions: {stats['total_workout_sessions']}\n"
        f"Best workout day: {stats['best_workout_volume_day']} ({stats['best_workout_volume']} volume)\n"
        "Correlation results:\n"
        f"{pair_lines_text}\n\n"
        "Return only the final report text."
    )

    return _call_report_llm(prompt)
