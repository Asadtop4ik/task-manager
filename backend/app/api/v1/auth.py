from fastapi import APIRouter, Cookie, HTTPException, Response, status

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import (
    TelegramAuthError,
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_init_data,
    verify_login_widget,
)
from app.db.models import User
from app.schemas.auth import AuthConfig, MiniAppLogin, TelegramWidgetLogin, TokenResponse
from app.schemas.user import UserOut
from app.services.auth_service import resolve_user

log = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"


def _issue(response: Response, user_id: int) -> TokenResponse:
    """Access token in the body, refresh token in an HttpOnly cookie.

    The refresh token is the long-lived credential, so it never touches
    JavaScript. The access token is short enough that keeping it in memory is
    the lesser risk.
    """
    response.set_cookie(
        REFRESH_COOKIE,
        create_refresh_token(user_id),
        max_age=settings.jwt_refresh_ttl_days * 86400,
        httponly=True,
        # Lax, not Strict: the Telegram Login Widget lands the user back here via
        # a top-level navigation, and Strict would drop the cookie on arrival.
        samesite="lax",
        secure=settings.environment == "production",
        path="/api/v1/auth",
    )
    return TokenResponse(
        access_token=create_access_token(user_id),
        expires_in=settings.jwt_access_ttl_minutes * 60,
    )


@router.get("/config", response_model=AuthConfig)
async def auth_config() -> AuthConfig:
    """What the login page needs to render the Telegram widget.

    Served at runtime rather than baked in as a Vite build arg, so the same
    frontend image works against any bot without a rebuild.
    """
    return AuthConfig(
        bot_username=settings.bot_username, login_enabled=bool(settings.bot_username)
    )


@router.post("/telegram", response_model=TokenResponse)
async def login_widget(
    payload: TelegramWidgetLogin, response: Response, session: DbSession
) -> TokenResponse:
    try:
        data = verify_login_widget(payload.to_payload())
    except TelegramAuthError as exc:
        log.warning("widget_login_rejected", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="telegram verification failed"
        ) from exc

    user = await resolve_user(session, data)
    await session.commit()

    if not user.is_active:
        # The account exists and is queued; say so plainly rather than 401, which
        # would send the UI back to a login button that will do nothing.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="account is not approved yet"
        )
    return _issue(response, user.id)


@router.post("/telegram/miniapp", response_model=TokenResponse)
async def login_miniapp(
    payload: MiniAppLogin, response: Response, session: DbSession
) -> TokenResponse:
    try:
        data = verify_init_data(payload.init_data)
    except TelegramAuthError as exc:
        log.warning("miniapp_login_rejected", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="telegram verification failed"
        ) from exc

    user = await resolve_user(session, data)
    await session.commit()

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="account is not approved yet"
        )
    return _issue(response, user.id)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    response: Response,
    session: DbSession,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
) -> TokenResponse:
    if refresh_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="no refresh cookie"
        )
    try:
        user_id = decode_token(refresh_token, "refresh")
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token"
        ) from exc

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        # Deactivating someone has to end their session, not just stop new logins.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="account is inactive"
        )
    return _issue(response, user.id)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
