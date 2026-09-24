from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, CheckConstraint, String, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.enums import UserRole

if TYPE_CHECKING:
    from app.db.models.project import Membership


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('manager', 'executor')",
            name="ck_users_role",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Telegram ids exceed 32 bits — BigInteger, not Integer.
    telegram_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default=UserRole.EXECUTOR, nullable=False)
    lang: Mapped[str] = mapped_column(String(8), default="uz", nullable=False)
    # Per user, not per server: a digest scheduled at 09:00 UTC arrives at 14:00 in Tashkent.
    tz: Mapped[str] = mapped_column(String(64), default="Asia/Tashkent", nullable=False)
    # A first-time telegram_id is stored inactive and waits for a manager. An open
    # Telegram login on a public domain is otherwise an open door.
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_use_codex: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User {self.id} tg={self.telegram_id} {self.role}>"
