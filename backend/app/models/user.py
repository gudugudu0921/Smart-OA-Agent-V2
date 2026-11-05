"""User ORM model."""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship

from app.db.mysql import Base


class User(Base):
    """An OA user, including the employee-to-manager reporting hierarchy."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("employee_no", name="uq_users_employee_no"),
        UniqueConstraint("username", name="uq_users_username"),
        UniqueConstraint("email", name="uq_users_email"),
        Index("idx_users_role", "role"),
        Index("idx_users_department", "department"),
        Index("idx_users_manager_id", "manager_id"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_no = Column(String(32), nullable=False)
    username = Column(String(64), nullable=False)
    password_hash = Column(String(255), nullable=False)
    name = Column(String(64), nullable=False)
    email = Column(String(128), nullable=True)
    role = Column(String(32), nullable=False)
    department = Column(String(64), nullable=False)
    position = Column(String(64), nullable=True)
    manager_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_users_manager_id_users"),
        nullable=True,
    )
    is_active = Column(
        Boolean,
        nullable=False,
        server_default=text("TRUE"),
    )
    must_change_password = Column(
        Boolean,
        nullable=False,
        server_default=text("FALSE"),
    )
    last_login_at = Column(DateTime, nullable=True)
    password_changed_at = Column(DateTime, nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        server_onupdate=text("CURRENT_TIMESTAMP"),
    )

    manager = relationship(
        "User",
        back_populates="direct_reports",
        remote_side="User.id",
        foreign_keys=[manager_id],
    )
    direct_reports = relationship(
        "User",
        back_populates="manager",
        foreign_keys="User.manager_id",
    )
    leave_applications = relationship(
        "LeaveApplication",
        back_populates="employee",
        foreign_keys="LeaveApplication.employee_id",
    )
    managed_leave_applications = relationship(
        "LeaveApplication",
        back_populates="manager",
        foreign_keys="LeaveApplication.manager_id",
    )
    hr_reviewed_leave_applications = relationship(
        "LeaveApplication",
        back_populates="hr",
        foreign_keys="LeaveApplication.hr_id",
    )
    notifications = relationship(
        "Notification",
        back_populates="user",
    )
    audit_logs = relationship(
        "AuditLog",
        back_populates="user",
    )
