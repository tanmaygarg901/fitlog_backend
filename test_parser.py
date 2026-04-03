import asyncio
from dotenv import load_dotenv

load_dotenv()

from app.models.schemas import ParseRequest
from app.routers.parse import parse_entry

TEST_USER_ID = "ea20c098-cade-4894-bf72-6f9480b095f3"


async def test():
    seed_cases = [
        "Push day: bench press 185 for 4x8, incline bench 60 for 3x10, overhead press 95 for 3x8, tricep pushdowns 3x12",
        "Leg day: squats 225 for 4x6, rdls 185 for 3x8, leg press 360 for 3x10",
        "Pull day: barbell rows 155 for 4x8, lat pulldowns 130 for 3x10, seated cable rows 120 for 3x12",
    ]

    print("Seeding workout templates/history...")
    for text in seed_cases:
        await parse_entry(ParseRequest(raw_text=text, user_id=TEST_USER_ID))

    test_cases = [
        "push day, went up 5 on bench and skipped incline",
        "legs but only squats and rdls, ran out of time",
        "pull day same as last time but added face pulls at the end",
    ]

    for i, text in enumerate(test_cases, 1):
        print(f"\n{'=' * 60}")
        print(f"TEST {i}: {text[:60]}...")
        print("=" * 60)

        result = await parse_entry(ParseRequest(raw_text=text, user_id=TEST_USER_ID))
        print(f"Type: {result.entry_type}")

        if result.meal:
            print(f"MEAL ({result.meal.meal_type}):")
            for item in result.meal.items:
                print(
                    f"  - {item.food_name} ({item.quantity_desc}): "
                    f"{item.calories}cal {item.protein_g}g protein [{item.source}]"
                )
            print(
                f"  TOTAL: {result.meal.total_calories}cal | "
                f"{result.meal.total_protein_g}g P | "
                f"{result.meal.total_carbs_g}g C | "
                f"{result.meal.total_fat_g}g F"
            )
            print(f"  Confidence: {result.meal.confidence}")

        if result.workout:
            print("WORKOUT:")
            for ex in result.workout.exercises:
                print(
                    f"  - {ex.exercise_name} ({ex.muscle_group}): "
                    f"{ex.sets}x{ex.reps} @ {ex.weight_lbs}lbs | "
                    f"volume={ex.volume_lbs}lbs | 1RM≈{ex.estimated_1rm}lbs"
                )
            print(f"  Total volume: {result.workout.total_volume_lbs} lbs")
            print(f"  Muscle groups: {result.workout.muscle_groups}")


asyncio.run(test())
