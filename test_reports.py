from datetime import date, timedelta

from app.db.supabase_client import supabase
from app.services.correlation import generate_weekly_report


def seed_daily_summaries(user_id: str) -> None:
    if supabase is None:
        raise RuntimeError("Supabase client is not initialized.")

    supabase.table("daily_summaries").delete().eq("user_id", user_id).execute()

    start_day = date.today() - timedelta(days=9)
    rows = []
    for i in range(10):
        day = start_day + timedelta(days=i)
        calories = 1950 + (i * 45)
        protein = 130 + (i * 3)
        carbs = 180 + (i * 6)
        fat = 55 + (i % 5) * 2
        workout_volume = 0 if i in {1, 4, 8} else 3200 + (i * 350)

        rows.append(
            {
                "user_id": user_id,
                "date": day.isoformat(),
                "total_calories": float(calories),
                "total_protein_g": float(protein),
                "total_carbs_g": float(carbs),
                "total_fat_g": float(fat),
                "total_volume_lbs": float(workout_volume),
                "workout_logged": workout_volume > 0,
            }
        )

    insert_resp = supabase.table("daily_summaries").insert(rows).execute()
    insert_error = getattr(insert_resp, "error", None)
    if insert_error:
        raise RuntimeError(f"Failed seeding daily_summaries: {insert_error}")

    insert_data = getattr(insert_resp, "data", None)
    if not insert_data:
        raise RuntimeError("Seed insert returned no data")


if __name__ == "__main__":
    test_user_id = "ea20c098-cade-4894-bf72-6f9480b095f3"
    seed_daily_summaries(test_user_id)

    report_text = generate_weekly_report(test_user_id)
    print("\nGenerated Weekly Report:\n")
    print(report_text)
