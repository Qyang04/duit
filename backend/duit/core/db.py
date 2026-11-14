"""Database engine and the declarative base every table model inherits from."""

from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, MetaData, Text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from duit.core.settings import Settings

# Stable, predictable constraint names, so later migrations can alter or drop them by name.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Python type -> column type defaults: all timestamps are timezone-aware.
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        str: Text(),
        bytes: LargeBinary(),
    }


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,  # drop dead connections after a Postgres restart or failover
    )
