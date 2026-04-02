from __future__ import annotations

from pydantic import BaseModel


class ParseRequest(BaseModel):
    raw_text: str
    user_id: str


class FoodItem(BaseModel):
    food_name: str
    quantity_desc: str
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float
    source: str


class MealEntry(BaseModel):
    meal_type: str
    total_calories: float
    total_protein_g: float
    total_carbs_g: float
    total_fat_g: float
    confidence: str
    items: list[FoodItem]


class ExerciseItem(BaseModel):
    exercise_name: str
    muscle_group: str
    sets: int
    reps: int
    weight_lbs: float
    volume_lbs: float
    estimated_1rm: float


class WorkoutEntry(BaseModel):
    muscle_groups: list[str]
    total_volume_lbs: float
    exercises: list[ExerciseItem]


class ParseResponse(BaseModel):
    entry_type: str
    meal: MealEntry | None = None
    workout: WorkoutEntry | None = None
    raw_text: str
