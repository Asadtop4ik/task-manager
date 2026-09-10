from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession, ManagerUser
from app.core.logging import get_logger
from app.db.models import Membership, Project, User
from app.schemas.project import MemberOut, ProjectCreate, ProjectOut, ProjectUpdate
from app.schemas.user import UserOut
from app.services.access import visible_project_ids

log = get_logger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])


async def _get_project(session: DbSession, project_id: int) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return project


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    session: DbSession, user: CurrentUser, include_archived: bool = False
) -> list[ProjectOut]:
    query = select(Project).order_by(Project.name)
    if not include_archived:
        query = query.where(Project.is_archived.is_(False))

    allowed = await visible_project_ids(session, user)
    if allowed is not None:
        query = query.where(Project.id.in_(allowed))

    rows = await session.scalars(query)
    return [ProjectOut.model_validate(row) for row in rows]


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate, session: DbSession, manager: ManagerUser
) -> ProjectOut:
    project = Project(**payload.model_dump())
    session.add(project)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"project key {payload.key!r} exists"
        ) from exc

    # The creator is a member from the start; a project nobody can see is a bug
    # waiting to be reported as one.
    session.add(
        Membership(user_id=manager.id, project_id=project.id, role_in_project=manager.role)
    )
    await session.commit()
    await session.refresh(project)
    log.info("project_created", project_id=project.id, key=project.key)
    return ProjectOut.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: int, payload: ProjectUpdate, session: DbSession, manager: ManagerUser
) -> ProjectOut:
    project = await _get_project(session, project_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    await session.commit()
    await session.refresh(project)
    return ProjectOut.model_validate(project)


@router.get("/{project_id}/members", response_model=list[MemberOut])
async def list_members(
    project_id: int, session: DbSession, user: CurrentUser
) -> list[MemberOut]:
    allowed = await visible_project_ids(session, user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")

    rows = await session.scalars(
        select(Membership)
        .where(Membership.project_id == project_id)
        .options(selectinload(Membership.user))
    )
    return [
        MemberOut(user=UserOut.model_validate(row.user), role_in_project=row.role_in_project)
        for row in rows
    ]


@router.put("/{project_id}/members/{user_id}", response_model=MemberOut)
async def add_member(
    project_id: int, user_id: int, session: DbSession, manager: ManagerUser
) -> MemberOut:
    await _get_project(session, project_id)
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    membership = await session.scalar(
        select(Membership).where(
            Membership.project_id == project_id, Membership.user_id == user_id
        )
    )
    if membership is None:
        membership = Membership(
            user_id=user_id, project_id=project_id, role_in_project=target.role
        )
        session.add(membership)
        await session.commit()
    return MemberOut(
        user=UserOut.model_validate(target), role_in_project=membership.role_in_project
    )


@router.delete("/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    project_id: int, user_id: int, session: DbSession, manager: ManagerUser
) -> None:
    membership = await session.scalar(
        select(Membership).where(
            Membership.project_id == project_id, Membership.user_id == user_id
        )
    )
    if membership is not None:
        await session.delete(membership)
        await session.commit()
