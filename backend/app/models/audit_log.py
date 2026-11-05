"""Audit log ORM model."""

from sqlalchemy import JSON, BigInteger, Column, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import relationship

from app.db.mysql import Base


class AuditLog(Base):
    """An immutable-style record of an action performed in the system."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("idx_audit_user_id", "user_id"),
        Index("idx_audit_action", "action"),
        Index("idx_audit_created_at", "created_at"),
        Index("idx_audit_request_id", "request_id"),
        Index("idx_audit_resource", "resource_type", "resource_id"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", name="fk_audit_logs_user_id_users"),
        nullable=True,
    )
    action = Column(String(64), nullable=False)
    resource_type = Column(String(64), nullable=False)
    resource_id = Column(String(64), nullable=True)
    detail = Column(JSON, nullable=True)
    ip_address = Column(String(45), nullable=True)
    request_id = Column(String(64), nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    user = relationship("User", back_populates="audit_logs")
