"""Seed local development users without duplicating existing accounts."""

from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import generate_temporary_password, hash_password
from app.db.mysql import SessionLocal
from app.models.user import User


BASE_USERS = (
    {
        "username": "admin",
        "employee_no": "ADMIN001",
        "name": "系统管理员",
        "role": "admin",
        "department": "管理部",
        "position": "系统管理员",
    },
    {
        "username": "manager_zhang",
        "employee_no": "MGR001",
        "name": "张主管",
        "role": "manager",
        "department": "技术部",
        "position": "部门主管",
    },
    {
        "username": "hr_liu",
        "employee_no": "HR001",
        "name": "刘HR",
        "role": "hr",
        "department": "人事部",
        "position": "HR专员",
    },
)

EMPLOYEE_USERS = (
    {
        "username": "employee_zhao",
        "employee_no": "EMP001",
        "name": "赵员工",
        "role": "employee",
        "department": "技术部",
        "position": "高级工程师",
    },
    {
        "username": "employee_qian",
        "employee_no": "EMP002",
        "name": "钱员工",
        "role": "employee",
        "department": "技术部",
        "position": "工程师",
    },
)


def _get_or_create_user(
    db: Session,
    user_data: dict,
    temporary_password: str,
    manager_id: int | None = None,
) -> tuple[User, bool]:
    existing = (
        db.query(User)
        .filter(
            or_(
                User.username == user_data["username"],
                User.employee_no == user_data["employee_no"],
            )
        )
        .all()
    )
    if existing:
        if len(existing) != 1 or (
            existing[0].username != user_data["username"]
            or existing[0].employee_no != user_data["employee_no"]
        ):
            raise RuntimeError(
                "Seed user conflicts with an existing username or employee number: "
                f"{user_data['username']} / {user_data['employee_no']}"
            )
        return existing[0], False

    user = User(
        **user_data,
        password_hash=hash_password(temporary_password),
        manager_id=manager_id,
        is_active=True,
        must_change_password=True,
        last_login_at=None,
        password_changed_at=None,
    )
    db.add(user)
    db.flush()
    return user, True


def _ensure_development_environment() -> None:
    if settings.app_env != "development":
        raise RuntimeError(
            "Development seed users may only be created when APP_ENV=development"
        )


def seed_users() -> None:
    _ensure_development_environment()
    db = SessionLocal()
    results = []
    try:
        base_users = {}
        for user_data in BASE_USERS:
            temporary_password = generate_temporary_password()
            user, created = _get_or_create_user(
                db,
                user_data,
                temporary_password,
            )
            base_users[user.username] = user
            results.append(
                (user.username, temporary_password if created else None)
            )

        manager = base_users["manager_zhang"]
        for user_data in EMPLOYEE_USERS:
            temporary_password = generate_temporary_password()
            user, created = _get_or_create_user(
                db,
                user_data,
                temporary_password,
                manager_id=manager.id,
            )
            results.append(
                (user.username, temporary_password if created else None)
            )

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print("Development seed credentials (shown once; store them securely):")
    for username, temporary_password in results:
        if temporary_password is None:
            print(f"{username}: already exists, password unchanged")
        else:
            print(f"{username}: {temporary_password}")
    print("These one-time credentials are for local development only.")


if __name__ == "__main__":
    seed_users()
