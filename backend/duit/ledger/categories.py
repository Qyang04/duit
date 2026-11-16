"""/categories: a two-level tree (e.g. Transport > Fuel), seeded with defaults at registration.

The list is flat (each item has parent_id); clients build the tree. Rules:
- Two levels: a parent must be top-level, and a category with children cannot move under
  another one.
- A subcategory always has its parent's is_income; changing it on a parent changes the children.
- Deleting a category un-categorises its transactions; one with children must be emptied first.
"""

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import exists, select, update

from duit.auth.models import User
from duit.auth.router import CurrentUser
from duit.core.deps import Db
from duit.ledger.common import Input, Name, Patch, flush_or_conflict, not_found, unknown
from duit.ledger.models import Category

router = APIRouter(prefix="/categories", tags=["categories"])

DUPLICATE_NAME = "A category with this name already exists at this level"


class CategoryIn(Input):
    name: Name
    parent_id: uuid.UUID | None = None
    is_income: bool = False  # ignored for subcategories: they take the parent's


class CategoryUpdate(Patch):
    required = ("name", "is_income")

    name: Name | None = None
    parent_id: uuid.UUID | None = None  # null makes it top-level
    is_income: bool | None = None


class CategoryOut(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    is_income: bool

    @classmethod
    def of(cls, c: Category) -> "CategoryOut":
        return cls(id=c.id, parent_id=c.parent_id, name=c.name, is_income=c.is_income)


def invalid(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


async def find_category(db: Db, user: User, category_id: uuid.UUID) -> Category | None:
    return await db.scalar(
        select(Category).where(Category.id == category_id, Category.user_id == user.id)
    )


async def get_category(db: Db, user: User, category_id: uuid.UUID) -> Category:
    category = await find_category(db, user, category_id)
    if category is None:
        raise not_found("Category")
    return category


async def has_children(db: Db, category: Category) -> bool:
    return bool(await db.scalar(select(exists().where(Category.parent_id == category.id))))


async def valid_parent(db: Db, user: User, parent_id: uuid.UUID) -> Category:
    parent = await find_category(db, user, parent_id)
    if parent is None:
        raise unknown("parent category")
    if parent.parent_id is not None:
        raise invalid("Categories are two levels deep: the parent must be a top-level category")
    return parent


@router.get("")
async def list_categories(user: CurrentUser, db: Db) -> list[CategoryOut]:
    categories = await db.scalars(
        select(Category).where(Category.user_id == user.id).order_by(Category.name)
    )
    return [CategoryOut.of(c) for c in categories]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_category(body: CategoryIn, user: CurrentUser, db: Db) -> CategoryOut:
    is_income = body.is_income
    if body.parent_id is not None:
        is_income = (await valid_parent(db, user, body.parent_id)).is_income
    category = Category(
        user_id=user.id, name=body.name, parent_id=body.parent_id, is_income=is_income
    )
    db.add(category)
    await flush_or_conflict(db, "uq_categories_user_id_parent_id_name", DUPLICATE_NAME)
    await db.commit()
    return CategoryOut.of(category)


@router.patch("/{category_id}")
async def update_category(
    category_id: uuid.UUID, body: CategoryUpdate, user: CurrentUser, db: Db
) -> CategoryOut:
    category = await get_category(db, user, category_id)
    changes: dict[str, Any] = body.changes()

    parent_id = changes.get("parent_id", category.parent_id)
    if parent_id is not None:  # it is, or becomes, a subcategory
        if parent_id == category.id:
            raise invalid("A category cannot be its own parent")
        if await has_children(db, category):
            raise invalid("A category with subcategories cannot become a subcategory")
        parent = await valid_parent(db, user, parent_id)
        if changes.get("is_income", parent.is_income) != parent.is_income:
            raise invalid("A subcategory takes is_income from its parent; change the parent")
        changes["is_income"] = parent.is_income
    elif "is_income" in changes:
        await db.execute(
            update(Category)
            .where(Category.user_id == user.id, Category.parent_id == category.id)
            .values(is_income=changes["is_income"])
        )

    for name, value in changes.items():
        setattr(category, name, value)
    await flush_or_conflict(db, "uq_categories_user_id_parent_id_name", DUPLICATE_NAME)
    await db.commit()
    return CategoryOut.of(category)


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(category_id: uuid.UUID, user: CurrentUser, db: Db) -> None:
    category = await get_category(db, user, category_id)
    if await has_children(db, category):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Category has subcategories; move or delete them first"
        )
    await db.delete(category)  # its transactions become uncategorised (ON DELETE SET NULL)
    await db.commit()
