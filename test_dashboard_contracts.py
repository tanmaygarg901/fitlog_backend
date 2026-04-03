import asyncio
import math
from datetime import date

from app.routers.dashboard import (
    get_muscle_distribution,
    get_prs,
    get_summary,
    get_weekly_macros,
    get_workout_volume,
)

TEST_USER_ID = "ea20c098-cade-4894-bf72-6f9480b095f3"


def _assert_iso_date(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise AssertionError(f"Expected non-empty ISO date string, got: {value!r}")
    date.fromisoformat(value)


def _assert_number(value: object, field_name: str) -> None:
    if value is None:
        raise AssertionError(f"{field_name} is None")
    if not isinstance(value, (int, float)):
        raise AssertionError(f"{field_name} is not numeric: {value!r}")
    if isinstance(value, float) and math.isnan(value):
        raise AssertionError(f"{field_name} is NaN")


async def test_weekly_macros_contract() -> None:
    data = await get_weekly_macros(user_id=TEST_USER_ID)
    days = data.get("days", [])

    assert len(days) == 7, f"Expected 7 days, got {len(days)}"

    for row in days:
        _assert_iso_date(row.get("date"))
        _assert_number(row.get("calories"), "calories")
        _assert_number(row.get("protein_g"), "protein_g")
        _assert_number(row.get("carbs_g"), "carbs_g")
        _assert_number(row.get("fat_g"), "fat_g")


async def test_workout_volume_contract() -> None:
    data = await get_workout_volume(user_id=TEST_USER_ID)
    days = data.get("days", [])

    assert len(days) == 14, f"Expected 14 days, got {len(days)}"

    for row in days:
        _assert_iso_date(row.get("date"))
        _assert_number(row.get("total_volume_lbs"), "total_volume_lbs")
        _assert_number(row.get("sets"), "sets")
        _assert_number(row.get("reps"), "reps")
        _assert_number(row.get("sessions"), "sessions")


async def test_muscle_distribution_contract() -> None:
    for window in (7, 14, 28):
        data = await get_muscle_distribution(user_id=TEST_USER_ID, window=window)
        assert data.get("window_days") == window, f"window_days mismatch for window={window}"
        by_group = data.get("by_group", [])
        for row in by_group:
            assert isinstance(row.get("muscle_group"), str), "muscle_group must be a string"
            _assert_number(row.get("volume_lbs"), "volume_lbs")
            _assert_number(row.get("percent"), "percent")


async def test_summary_contract() -> None:
    data = await get_summary(user_id=TEST_USER_ID)

    for key in ("calories", "protein_g", "workouts_this_week", "logging_streak_days"):
        assert key in data, f"Missing summary key: {key}"
        value = data[key]
        assert isinstance(value, int), f"Summary key {key} must be integer, got {type(value)}"
        assert value >= 0, f"Summary key {key} must be non-negative"


async def test_prs_contract() -> None:
    data = await get_prs(user_id=TEST_USER_ID)
    prs = data.get("prs", [])

    for row in prs:
        name = row.get("exercise_name", "")
        assert isinstance(name, str) and name, "exercise_name must be a non-empty string"
        assert name == name.title(), f"exercise_name must be title case: {name}"

        delta = row.get("delta_lbs")
        if delta is not None:
            assert isinstance(delta, (int, float)), f"delta_lbs must be null or number, got: {delta!r}"


async def main() -> None:
    await test_weekly_macros_contract()
    print("PASS: weekly-macros contract")

    await test_workout_volume_contract()
    print("PASS: workout-volume contract")

    await test_muscle_distribution_contract()
    print("PASS: muscle-distribution contract")

    await test_summary_contract()
    print("PASS: summary contract")

    await test_prs_contract()
    print("PASS: prs contract")

    print("All dashboard contract tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
