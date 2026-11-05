"""Explicit, one-time bootstrap command for the first administrator."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import generate_temporary_password, hash_password
from app.db.mysql import SessionLocal
from app.models.user import User
from app.schemas.user import UserCreate, UserRole


class BootstrapAdminError(RuntimeError):
    """Raised when the database is not eligible for administrator bootstrap."""


@dataclass(frozen=True)
class BootstrapAdminInput:
    username: str
    employee_no: str
    name: str
    email: str


def create_bootstrap_admin(
    db: Session,
    payload: BootstrapAdminInput,
) -> tuple[User, str]:
    """Create the first administrator in an otherwise empty users table.

    The plaintext password is returned to the command entry point for one-time
    display. It is never logged, audited, or persisted.
    """
    try:
        # A locking read makes concurrent bootstrap attempts serialize on MySQL.
        existing_users = (
            db.query(User)
            .order_by(User.id)
            .with_for_update()
            .all()
        )
        if any(user.role == UserRole.ADMIN.value for user in existing_users):
            raise BootstrapAdminError("ADMIN_ALREADY_EXISTS")
        if existing_users:
            raise BootstrapAdminError("NOT_FRESH_INSTALL")

        validated = UserCreate(
            username=payload.username,
            employee_no=payload.employee_no,
            name=payload.name,
            email=payload.email,
            role=UserRole.ADMIN,
            department="Administration",
            position="System Administrator",
            manager_id=None,
        )
        temporary_password = generate_temporary_password()
        administrator = User(
            username=validated.username,
            employee_no=validated.employee_no,
            password_hash=hash_password(temporary_password),
            name=validated.name,
            email=str(validated.email).lower(),
            role=UserRole.ADMIN.value,
            department=validated.department,
            position=validated.position,
            manager_id=None,
            is_active=True,
            must_change_password=True,
            last_login_at=None,
            password_changed_at=None,
        )
        db.add(administrator)
        db.flush()
        db.commit()
    except BootstrapAdminError:
        db.rollback()
        raise
    except ValidationError as exc:
        db.rollback()
        raise BootstrapAdminError("BOOTSTRAP_INPUT_INVALID") from exc
    except IntegrityError as exc:
        db.rollback()
        raise BootstrapAdminError("BOOTSTRAP_IDENTITY_CONFLICT") from exc
    except Exception:
        db.rollback()
        raise

    return administrator, temporary_password


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the first Smart OA administrator on a fresh install.",
    )
    parser.add_argument("--username")
    parser.add_argument("--employee-no")
    parser.add_argument("--name")
    parser.add_argument("--email")
    return parser


def _required(value: str | None, prompt: str) -> str:
    result = value if value is not None else input(prompt)
    result = result.strip()
    if not result:
        raise BootstrapAdminError("BOOTSTRAP_INPUT_REQUIRED")
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = BootstrapAdminInput(
            username=_required(args.username, "Username: "),
            employee_no=_required(args.employee_no, "Employee number: "),
            name=_required(args.name, "Name: "),
            email=_required(args.email, "Email: "),
        )
        db = SessionLocal()
        try:
            administrator, temporary_password = create_bootstrap_admin(db, payload)
        finally:
            db.close()
    except BootstrapAdminError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("BOOTSTRAP_FAILED", file=sys.stderr)
        return 1

    print("Bootstrap administrator created successfully.")
    print()
    print(f"Username: {administrator.username}")
    print(f"Temporary password: {temporary_password}")
    print()
    print("This temporary password is shown only once.")
    print("The administrator must change it after first login.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
