"""FastAPI dependencies shared by every router."""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from duit.core.settings import Settings, get_settings


async def get_db(request: Request) -> AsyncGenerator[AsyncSession]:
    """One database session per request, from the pool opened in the app lifespan."""
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        yield session


Db = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
