"""Leave application ORM model."""

from sqlalchemy import Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, text
from sqlalchemy.orm import relationship

from app.db.mysql import Base


class LeaveApplication(Base):
    """A leave request and its manager and HR review state."""

    __tablename__ = "leave_applications"
    __table_args__ = (
        Index("idx_leave_employee_id", "employee_id"),
        Index("idx_leave_status", "status"),
        Index("idx_leave_manager_status", "manager_status"),
        Index("idx_leave_hr_status", "hr_status"),
        Index("idx_leave_start_date", "start_date"),
        Index("idx_leave_created_at", "created_at"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(String(36), primary_key=True)
    employee_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_leave_applications_employee_id_users"),
        nullable=False,
    )
    leave_type = Column(String(32), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    leave_days = Column(Numeric(5, 1), nullable=False)
    reason = Column(Text, nullable=True)
    status = Column(
        String(32),
        nullable=False,
        server_default=text("'pending'"),
    )
    manager_status = Column(
        String(32),
        nullable=False,
        server_default=text("'pending'"),
    )
    manager_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_leave_applications_manager_id_users"),
        nullable=True,
    )
    manager_comment = Column(Text, nullable=True)
    manager_approved_at = Column(DateTime, nullable=True)
    hr_status = Column(
        String(32),
        nullable=False,
        server_default=text("'pending'"),
    )
    hr_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_leave_applications_hr_id_users"),
        nullable=True,
    )
    hr_comment = Column(Text, nullable=True)
    hr_approved_at = Column(DateTime, nullable=True)
    submitted_at = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    cancelled_at = Column(DateTime, nullable=True)
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

    employee = relationship(
        "User",
        back_populates="leave_applications",
        foreign_keys=[employee_id],
    )
    manager = relationship(
        "User",
        back_populates="managed_leave_applications",
        foreign_keys=[manager_id],
    )
    hr = relationship(
        "User",
        back_populates="hr_reviewed_leave_applications",
        foreign_keys=[hr_id],
    )
    notifications = relationship(
        "Notification",
        back_populates="leave_application",
    )
