import hmac
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import TokenError, decode_token
from app.db.models import User
from app.db.session import get_db
from app.services.access import is_manager

log = get_logger(__name__)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async for session in get_db():
        yield session


DbSession = Annotated[AsyncSession, Depends(db_session)]


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def current_user(
    request: Request,
    session: DbSession,
    x_service_token: Annotated[str | None, Header()] = None,
    x_acting_user: Annotated[int | None, Header()] = None,
) -> User:
    """The person behind this request, whether they came from the web or the bot.

    The bot is a first-party client, not a user: it presents SERVICE_TOKEN and
    names the verified telegram_id it is acting for. Attribution and permission
    checks then run identically for both clients, so there is one set of rules
    rather than a web set and a bot set that drift apart.
    """
    user: User | None = None

    if x_service_token is not None:
        if not hmac.compare_digest(x_service_token, settings.service_token):
            log.warning("service_token_rejected")
            raise _UNAUTHORIZED
        if x_acting_user is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Acting-User is required with a service token",
            )
        user = await session.scalar(select(User).where(User.telegram_id == x_acting_user))
    else:
        token = _bearer_token(request)
        if token is None:
            raise _UNAUTHORIZED
        try:
            user_id = decode_token(token, "access")
        except TokenError as exc:
            log.info("access_token_rejected", error=str(exc))
            raise _UNAUTHORIZED from exc
        user = await session.get(User, user_id)

    if user is None:
        raise _UNAUTHORIZED
    if not user.is_active:
        # Approval is pending, or someone was deactivated. Distinct from 401 so
        # the UI can say "waiting for approval" instead of "log in again".
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="account is not approved yet"
        )
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def manager_user(user: CurrentUser) -> User:
    if not is_manager(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="manager role required"
        )
    return user


ManagerUser = Annotated[User, Depends(manager_user)]


async def owner_user(user: CurrentUser) -> User:
    if not settings.owner_telegram_id or user.telegram_id != settings.owner_telegram_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="owner access required"
        )
    return user


OwnerUser = Annotated[User, Depends(owner_user)]
