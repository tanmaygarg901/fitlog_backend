import asyncio
from pprint import pprint

from app.models.schemas import ParseRequest
from app.routers.parse import parse_entry


async def run_case(name: str, raw_text: str, user_id: str) -> None:
    print("\n" + "=" * 70)
    print(name)
    print("=" * 70)

    request = ParseRequest(raw_text=raw_text, user_id=user_id)
    response = await parse_entry(request)

    print("ParseResponse:")
    pprint(response.model_dump())


async def main() -> None:
    user_id = "ea20c098-cade-4894-bf72-6f9480b095f3"

    await run_case(
        name="CASE 1 - MEAL",
        raw_text="Had 2 eggs, toast, and orange juice for breakfast",
        user_id=user_id,
    )

    await run_case(
        name="CASE 2 - WORKOUT",
        raw_text="Leg day: squats 225 4x6 and lunges 3x10",
        user_id=user_id,
    )

    await run_case(
        name="CASE 3 - BOTH",
        raw_text="Had chicken and rice for lunch, then did bench press 185 for 4x8",
        user_id=user_id,
    )


if __name__ == "__main__":
    asyncio.run(main())
