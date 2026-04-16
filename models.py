import uuid
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


class Clearance(Base):
    __tablename__ = "clearances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(64), index=True)
    employee_name: Mapped[str] = mapped_column(String(200))
    position: Mapped[str] = mapped_column(String(200))
    division_department: Mapped[str] = mapped_column(String(200))
    date_hired: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    date_separated: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    date_prepared: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    reason_for_separation: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    payroll_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    payroll_class: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    first_printed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_printed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    print_count: Mapped[int] = mapped_column(Integer, default=0)

    approvers: Mapped[list["FormApprover"]] = relationship(
        back_populates="clearance", cascade="all, delete-orphan", lazy="selectin"
    )


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    approvers: Mapped[list["FormApprover"]] = relationship(back_populates="department")


class SignInLog(Base):
    __tablename__ = "sign_in_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)
    domain: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    signed_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class EmailSendLog(Base):
    __tablename__ = "email_send_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clearance_id: Mapped[Optional[int]] = mapped_column(ForeignKey("clearances.id", ondelete="SET NULL"), nullable=True, index=True)
    department_id: Mapped[Optional[int]] = mapped_column(ForeignKey("departments.id", ondelete="SET NULL"), nullable=True, index=True)
    to_email: Mapped[str] = mapped_column(String(200), index=True)
    subject: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    department: Mapped[Optional["Department"]] = relationship(lazy="selectin")


class HrAccessUser(Base):
    __tablename__ = "hr_access_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FormApprover(Base):
    __tablename__ = "form_approvers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clearance_id: Mapped[int] = mapped_column(ForeignKey("clearances.id", ondelete="CASCADE"), index=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id", ondelete="RESTRICT"), index=True)
    sign_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, default=uuid.uuid4, index=True)

    accountability_item: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    accountability_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)

    signer_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    signature_data_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    disapproval_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    disapproved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    clearance: Mapped["Clearance"] = relationship(back_populates="approvers", lazy="selectin")
    department: Mapped["Department"] = relationship(back_populates="approvers", lazy="selectin")

