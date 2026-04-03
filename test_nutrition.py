import asyncio
from dotenv import load_dotenv
load_dotenv()

from app.services.nutrition import lookup_nutrition

async def test():
    items = [
        ("grilled chicken breast", "1 large chicken breast"),
        ("brown rice", "1 cup cooked"),
        ("banana", "1 medium banana"),
        ("Chipotle chicken burrito bowl", "1 bowl"),  # should hit LLM fallback
    ]
    for food, qty in items:
        result = await lookup_nutrition(food, qty)
        print(f"\n{food} ({qty})")
        print(f"  Calories: {result.calories} | Protein: {result.protein_g}g | "
              f"Carbs: {result.carbs_g}g | Fat: {result.fat_g}g | "
              f"Source: {result.source}")

asyncio.run(test())
