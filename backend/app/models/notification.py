"""Notification ORM model."""

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import relationship

from app.db.mysql import Base


class Notification(Base):
    """A notification delivered to an OA user."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("idx_notification_user_id", "user_id"),
        Index("idx_notification_is_read", "is_read"),
        Index("idx_notification_created_at", "created_at"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_notifications_user_id_users"),
        nullable=False,
    )
    type = Column(String(32), nullable=False)
    title = Column(String(128), nullable=False)
    content = Column(Text, nullable=False)
    related_application_id = Column(
        String(36),
        ForeignKey(
            "leave_applications.id",
            name="fk_notifications_related_application_id_leave_applications",
        ),
        nullable=True,
    )
    is_read = Column(
        Boolean,
        nullable=False,
        server_default=text("FALSE"),
    )
    read_at = Column(DateTime, nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    user = relationship("User", back_populates="notifications")
    leave_application = relationship(
        "LeaveApplication",
        back_populates="notifications",
    )
