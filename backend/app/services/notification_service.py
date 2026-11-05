"""In-app notification writes, owned reads, and MySQL-backed todos."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.leave_application import LeaveApplication
from app.models.notification import Notification
from app.models.user import User


LEAVE_TYPE_LABELS = {
    "annual": "年假",
    "sick": "病假",
    "personal": "事假",
    "marriage": "婚假",
    "maternity": "产假",
    "bereavement": "丧假",
    "other": "其他请假",
}


class NotificationNotFoundError(LookupError):
    """Raised for both missing notifications and ownership mismatches."""


def create_notification(
    db: Session,
    *,
    user_id: int,
    notification_type: str,
    title: str,
    content: str,
    related_application_id: str | None = None,
) -> Notification:
    """Add a notification to the caller's transaction without committing."""
    notification = Notification(
        user_id=user_id,
        type=notification_type,
        title=title,
        content=content,
        related_application_id=related_application_id,
        is_read=False,
    )
    db.add(notification)
    return notification


def _leave_summary(application: LeaveApplication) -> str:
    leave_type = LEAVE_TYPE_LABELS.get(application.leave_type, application.leave_type)
    return f"{application.start_date.isoformat()} 至 {application.end_date.isoformat()} 的{leave_type}申请"


def _comment_suffix(label: str, comment: str | None) -> str:
    normalized = (comment or "").strip()
    return f"，{label}：{normalized}" if normalized else ""


def notify_leave_created(
    db: Session,
    application: LeaveApplication,
    employee: User,
) -> None:
    if application.manager_id is None:
        return
    create_notification(
        db,
        user_id=application.manager_id,
        notification_type="leave_created",
        title="新的请假审批",
        content=f"{employee.name}提交了{_leave_summary(application)}，请及时审批。",
        related_application_id=application.id,
    )


def notify_manager_reviewed(
    db: Session,
    application: LeaveApplication,
    review_comment: str | None,
) -> None:
    if application.manager_status == "approved":
        create_notification(
            db,
            user_id=application.employee_id,
            notification_type="leave_manager_approved",
            title="主管审批已通过",
            content=(
                f"你的{_leave_summary(application)}已通过主管审批，正在等待 HR 审批"
                f"{_comment_suffix('主管审批意见', review_comment)}。"
            ),
            related_application_id=application.id,
        )
        employee = db.get(User, application.employee_id)
        employee_name = employee.name if employee is not None else f"员工 #{application.employee_id}"
        active_hr_users = (
            db.query(User)
            .filter(User.role == "hr", User.is_active.is_(True))
            .all()
        )
        for hr_user in active_hr_users:
            create_notification(
                db,
                user_id=hr_user.id,
                notification_type="leave_hr_pending",
                title="新的 HR 审批",
                content=f"{employee_name}的{_leave_summary(application)}已通过主管审批，请进行 HR 审批。",
                related_application_id=application.id,
            )
        return

    create_notification(
        db,
        user_id=application.employee_id,
        notification_type="leave_manager_rejected",
        title="请假申请未通过主管审批",
        content=(
            f"你的{_leave_summary(application)}未通过主管审批"
            f"{_comment_suffix('主管审批意见', review_comment)}。"
        ),
        related_application_id=application.id,
    )


def notify_hr_reviewed(
    db: Session,
    application: LeaveApplication,
    review_comment: str | None,
) -> None:
    if application.hr_status == "approved":
        title = "请假申请已批准"
        content = (
            f"你的{_leave_summary(application)}已完成审批并通过"
            f"{_comment_suffix('HR审批意见', review_comment)}。"
        )
        notification_type = "leave_hr_approved"
    else:
        title = "请假申请未通过 HR 审批"
        content = (
            f"你的{_leave_summary(application)}最终未通过 HR 审批"
            f"{_comment_suffix('HR审批意见', review_comment)}。"
        )
        notification_type = "leave_hr_rejected"
    create_notification(
        db,
        user_id=application.employee_id,
        notification_type=notification_type,
        title=title,
        content=content,
        related_application_id=application.id,
    )


def get_notifications(
    db: Session,
    user_id: int,
    *,
    unread_only: bool = False,
    skip: int = 0,
    limit: int = 50,
) -> list[Notification]:
    query = db.query(Notification).filter(Notification.user_id == user_id)
    if unread_only:
        query = query.filter(Notification.is_read.is_(False))
    return query.order_by(Notification.created_at.desc(), Notification.id.desc()).offset(skip).limit(limit).all()


def get_unread_count(db: Session, user_id: int) -> int:
    return (
        db.query(Notification)
        .filter(Notification.user_id == user_id, Notification.is_read.is_(False))
        .count()
    )


def mark_notification_read(db: Session, user_id: int, notification_id: int) -> Notification:
    notification = (
        db.query(Notification)
        .filter(Notification.id == notification_id, Notification.user_id == user_id)
        .first()
    )
    if notification is None:
        raise NotificationNotFoundError("Notification not found")
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = datetime.now()
        try:
            db.commit()
            db.refresh(notification)
        except Exception:
            db.rollback()
            raise
    return notification


def mark_all_notifications_read(db: Session, user_id: int) -> int:
    now = datetime.now()
    try:
        updated = (
            db.query(Notification)
            .filter(Notification.user_id == user_id, Notification.is_read.is_(False))
            .update({Notification.is_read: True, Notification.read_at: now}, synchronize_session=False)
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return updated


def get_todos(db: Session, user: User) -> dict[str, object]:
    manager_approvals = 0
    hr_approvals = 0
    if user.role == "manager":
        manager_approvals = (
            db.query(LeaveApplication)
            .filter(
                LeaveApplication.manager_id == user.id,
                LeaveApplication.manager_status == "pending",
                LeaveApplication.status == "pending",
            )
            .count()
        )
    elif user.role == "hr":
        hr_approvals = (
            db.query(LeaveApplication)
            .filter(
                LeaveApplication.manager_status == "approved",
                LeaveApplication.hr_status == "pending",
                LeaveApplication.status == "pending",
            )
            .count()
        )
    return {
        "role": user.role,
        "total": manager_approvals + hr_approvals,
        "items": {
            "manager_approvals": manager_approvals,
            "hr_approvals": hr_approvals,
        },
    }
