"""数据模型包。"""

from app.models.dish import Canteen, Dish, Nutrition
from app.models.enums import (
    Allergen,
    Category,
    Cuisine,
    DietaryTag,
    Flavor,
    MealPeriod,
)
from app.models.preference import UserPreference
from app.models.recommendation import (
    MealPlan,
    Recommendation,
    RecommendResponse,
    ScoreBreakdown,
)

__all__ = [
    "Allergen",
    "Canteen",
    "Category",
    "Cuisine",
    "DietaryTag",
    "Dish",
    "Flavor",
    "MealPeriod",
    "MealPlan",
    "Nutrition",
    "Recommendation",
    "RecommendResponse",
    "ScoreBreakdown",
    "UserPreference",
]
