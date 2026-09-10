from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.task import Task
    from app.db.models.user import User


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Short handle the bot's quick-capture parser matches on ("keto: fix the url").
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    color: Mapped[str] = mapped_column(String(16), default="#64748b", nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["Task"]] = relationship(back_populates="project")

    def __repr__(self) -> str:
        return f"<Project {self.key}>"


class Membership(Base, TimestampMixin):
    """Who sees which project. Adding a third person is a row, not a rewrite."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "project_id", name="uq_membership"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role_in_project: Mapped[str] = mapped_column(
        String(16), default="executor", nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="memberships")
    project: Mapped["Project"] = relationship(back_populates="memberships")
