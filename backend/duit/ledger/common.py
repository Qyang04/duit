"""Pieces shared by the ledger endpoints."""

from typing import Annotated, Any, ClassVar, Self

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$", examples=["MYR"])]
Name = Annotated[str, Field(min_length=1, max_length=100)]


class Input(BaseModel):
    """Request bodies reject unknown fields, so a client cannot slip in user_id, dedupe_key or
    any other column the endpoint does not offer (mass assignment, OWASP API Top 10)."""

    model_config = ConfigDict(extra="forbid")


class Patch(Input):
    """PATCH bodies: a field left out stays as it is, an explicit null clears it. Fields named
    in `required` are NOT NULL columns, so null is rejected for them."""

    required: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="after")
    def _required_not_null(self) -> Self:
        for name in self.required:
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self

    def changes(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.model_fields_set}


def not_found(what: str) -> HTTPException:
    """Another user's row gets the same 404 as a missing one: a 403 would confirm that the id
    exists (broken object level authorization is OWASP API #1)."""
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")


def unknown(what: str) -> HTTPException:
    """A reference in the request body (not the URL) that the user does not own."""
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown {what}")


async def flush_or_conflict(db: AsyncSession, constraint: str, detail: str) -> None:
    """Flush, turning a violation of one named unique constraint into 409. The database is the
    source of truth: checking first with a SELECT would still race with a concurrent insert."""
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if constraint in str(exc.orig):
            raise HTTPException(status.HTTP_409_CONFLICT, detail) from exc
        raise
