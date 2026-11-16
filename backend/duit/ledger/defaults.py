"""Default categories every new user starts with. Data, not logic: edit freely.

Migrations keep their own frozen copy (they must not import app code, which keeps changing),
so changing this list affects new users only.
"""

import uuid

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from duit.ledger.models import Category

# (parent, is_income, children)
DEFAULT_CATEGORIES: tuple[tuple[str, bool, tuple[str, ...]], ...] = (
    ("Income", True, ("Salary", "Bonus", "Interest & dividends", "Refunds", "Other income")),
    ("Food & drink", False, ("Groceries", "Eating out", "Food delivery")),
    ("Transport", False, ("Fuel", "Toll & parking", "E-hailing", "Public transport", "Car")),
    ("Housing", False, ("Rent & mortgage", "Utilities", "Internet & phone", "Household")),
    ("Shopping", False, ("Clothing", "Electronics", "Online shopping")),
    ("Health", False, ("Medical", "Insurance", "Fitness")),
    ("Lifestyle", False, ("Entertainment", "Subscriptions", "Personal care", "Hobbies")),
    ("Education", False, ("Courses", "Books")),
    ("Family & giving", False, ("Parents", "Children", "Gifts", "Donations & zakat")),
    ("Travel", False, ("Flights", "Accommodation")),
    ("Financial", False, ("Bank fees", "Loan repayment", "Income tax")),
)


async def seed_default_categories(db: AsyncSession, user_id: uuid.UUID) -> None:
    for parent_name, is_income, children in DEFAULT_CATEGORIES:
        parent_id = await db.scalar(
            insert(Category)
            .values(user_id=user_id, name=parent_name, is_income=is_income)
            .returning(Category.id)
        )
        await db.execute(
            insert(Category),
            [
                {"user_id": user_id, "parent_id": parent_id, "name": c, "is_income": is_income}
                for c in children
            ],
        )
