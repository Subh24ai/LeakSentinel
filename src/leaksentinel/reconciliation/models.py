"""SQLAlchemy ORM models for the reconciliation domain.

These tables model both *sides* of the commission picture:

* The **expected** side — what we believe we are owed: ``dealers`` ->
  ``sales`` -> ``policies`` (with an ``expected_commission_rate``), plus the
  distributor's own ``crm_records``.
* The **actual** side — ``insurer_commission_feeds``, the (intentionally
  messy) statements insurers send describing what they actually paid us.

``reconciliation_results`` holds the per-policy verdict after matching the two
sides, and ``audit_log`` records every mutating action for traceability.

All models hang off the shared declarative ``Base`` from :mod:`leaksentinel.db`,
so importing this module is enough to register them on ``Base.metadata``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from leaksentinel.db import Base


class Dealer(Base):
    """An OEM dealer through which RSA subscriptions and policies are sold."""

    __tablename__ = "dealers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    oem: Mapped[str | None] = mapped_column(String(128))
    city: Mapped[str | None] = mapped_column(String(128))

    sales: Mapped[list["Sale"]] = relationship(back_populates="dealer")


class Sale(Base):
    """A single point-of-sale event at a dealer.

    A sale may bundle an RSA subscription and/or an insurance policy. The
    sale<->policy link is owned solely by ``policies.sale_id`` (see ``Policy``);
    a sale reaches its policies via the ``policies`` back-reference.
    """

    __tablename__ = "sales"

    id: Mapped[int] = mapped_column(primary_key=True)
    dealer_id: Mapped[int | None] = mapped_column(ForeignKey("dealers.id"))
    customer_name: Mapped[str | None] = mapped_column(String(255))
    vehicle_reg: Mapped[str | None] = mapped_column(String(32), index=True)
    sale_date: Mapped[dt.date | None] = mapped_column()
    rsa_subscription_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # SureDrive's flagship "1+1" free-renewal: the customer is owed a second
    # year. ``eligible`` means we promised it; ``provisioned`` means we actually
    # booked it. eligible & not provisioned => leakage owed to the customer.
    renewal_1plus1_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    renewal_provisioned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    dealer: Mapped["Dealer | None"] = relationship(back_populates="sales")
    policies: Mapped[list["Policy"]] = relationship(back_populates="sale")


class Policy(Base):
    """An insurance policy we sold on behalf of an insurer.

    This is the *expected* side: ``premium`` * ``expected_commission_rate``
    is what we believe the insurer owes us.
    """

    __tablename__ = "policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    insurer_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # UNIQUE: reconciliation joins on policy_no; duplicates would corrupt results.
    policy_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    premium: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    expected_commission_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    # Single source of truth for the sale<->policy link.
    sale_id: Mapped[int | None] = mapped_column(ForeignKey("sales.id"))
    issued_date: Mapped[dt.date | None] = mapped_column()

    sale: Mapped["Sale | None"] = relationship(back_populates="policies")


class InsurerCommissionFeed(Base):
    """A raw line item from an insurer's commission statement.

    This is the *actual* side and is intentionally messy: ``raw_policy_ref``
    may not cleanly match ``policies.policy_no``, ``status`` is free text, and
    amounts/dates can be missing. Feeds are normalized into a
    :class:`~leaksentinel.reconciliation.schemas.ReconciliationView` before
    matching.
    """

    __tablename__ = "insurer_commission_feeds"

    id: Mapped[int] = mapped_column(primary_key=True)
    insurer_name: Mapped[str | None] = mapped_column(String(128), index=True)
    raw_policy_ref: Mapped[str | None] = mapped_column(String(128), index=True)
    commission_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    paid_date: Mapped[dt.date | None] = mapped_column()
    status: Mapped[str | None] = mapped_column(String(64))  # raw, untranslated

    # --- Fields produced by the ingestion normalizer ---------------------- #
    # Normalized policy number (reconciliation joins on this). Nullable: an
    # unparseable ref is still persisted, with the reason in normalization_notes.
    policy_no: Mapped[str | None] = mapped_column(String(64), index=True)
    premium: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    payment_status: Mapped[str | None] = mapped_column(String(32))  # canonical enum value
    normalization_notes: Mapped[str | None] = mapped_column(Text)  # JSON list, or NULL


class CrmRecord(Base):
    """The distributor's own CRM view of a policy's commission."""

    __tablename__ = "crm_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    policy_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    recorded_commission: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    recorded_date: Mapped[dt.date | None] = mapped_column()


class ReconciliationResult(Base):
    """The per-policy verdict after reconciling expected vs. actual."""

    __tablename__ = "reconciliation_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    policy_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str | None] = mapped_column(String(32), index=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), index=True)
    expected: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    actual: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    delta: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolution_state: Mapped[str | None] = mapped_column(String(32), index=True)


class AuditLog(Base):
    """Append-only record of every mutating action taken by the engine."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_sha256: Mapped[str | None] = mapped_column(String(64))
    actor: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    detail: Mapped[str | None] = mapped_column(Text)
