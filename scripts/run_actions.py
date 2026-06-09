"""Demo run of the actions layer end-to-end, then dump the audit trail.

Self-contained: spins up an in-memory SQLite database (no Postgres / no synthetic
data required) and walks one finding through each governance path —

  * a valid claim (gated -> acted -> audited),
  * a retry of that same claim (idempotent: no second row),
  * a too-small finding (refused, not escalated),
  * an unconfirmed finding (refused -> escalated to a human),
  * a high-value finding (auto-claim skipped -> escalated to a human),

then prints commission_claims, escalations, and the full audit_log so you can see
the SHA-256-stamped trail of every action AND every refusal.

    python scripts/run_actions.py
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from leaksentinel.actions.escalation import Escalation, route
from leaksentinel.actions.externalapi import ExternalClaimsAPI
from leaksentinel.actions.remediation import (
    ActionableFinding,
    CommissionClaim,
    create_commission_claim,
)
from leaksentinel.db import Base
from leaksentinel.detection.rules import DetectionReason
from leaksentinel.reconciliation.models import AuditLog

MISSING = DetectionReason.MISSING_COMMISSION.value


def _finding(policy_no, amount, *, status="open", reason=MISSING):
    return ActionableFinding(
        policy_no=policy_no,
        reason_code=reason,
        amount=Decimal(amount),
        status=status,
        detail="planted leak (demo)",
    )


def main() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    api = ExternalClaimsAPI()

    print("=== Running sample remediation / escalation flows ===\n")

    # 1. Valid claim — gated, acted, audited.
    f1 = _finding("POL-001", "742.50")
    out = create_commission_claim(f1, session=session, api=api)
    print(f"POL-001  valid claim        -> {out.status.value:9}  claim#{out.claim.id}")

    # 2. Retry the SAME claim — idempotent, no second row.
    out = create_commission_claim(f1, session=session, api=api)
    print(f"POL-001  retry (idempotent) -> {out.status.value:9}  claim#{out.claim.id}")

    # 3. Too small — refused, NOT escalated.
    out = route(_finding("POL-002", "12.00"), session=session, api=api)
    print(f"POL-002  too small          -> blocked    [{out.decision.code.value}]")

    # 4. Not a confirmed open leak — refused, escalated to a human.
    out = route(_finding("POL-003", "900.00", status="resolved"), session=session, api=api)
    print(f"POL-003  not confirmed      -> escalated  [{out.reason.value}]  esc#{out.escalation.id}")  # noqa: E501

    # 5. High value — auto-claim skipped, escalated to a human.
    out = route(_finding("POL-004", "8500.00"), session=session, api=api)
    print(f"POL-004  high value         -> escalated  [{out.reason.value}]  esc#{out.escalation.id}")  # noqa: E501

    session.commit()

    # --- commission_claims ------------------------------------------------- #
    print("\n=== commission_claims ===")
    print(
        f"{'id':>3}  {'policy_no':<9}  {'amount':>10}  {'status':<10}  "
        f"{'external_ref':<22}  idem_key[:12]"
    )
    for c in session.execute(select(CommissionClaim).order_by(CommissionClaim.id)).scalars():
        print(
            f"{c.id:>3}  {c.policy_no:<9}  {str(c.claim_amount):>10}  {c.status:<10}  "
            f"{(c.external_ref or ''):<22}  {c.idempotency_key[:12]}"
        )

    # --- escalations ------------------------------------------------------- #
    print("\n=== escalations (human queue) ===")
    print(f"{'id':>3}  {'policy_no':<9}  {'amount':>10}  {'escalation_reason':<22}  status")
    for e in session.execute(select(Escalation).order_by(Escalation.id)).scalars():
        print(
            f"{e.id:>3}  {e.policy_no:<9}  {str(e.amount):>10}  "
            f"{e.escalation_reason:<22}  {e.status}"
        )

    # --- audit_log (the immutable trail) ----------------------------------- #
    print("\n=== audit_log (every action AND every refusal, SHA-256 stamped) ===")
    print(f"{'id':>3}  {'action':<36}  {'sha256[:16]':<16}  detail")
    for a in session.execute(select(AuditLog).order_by(AuditLog.id)).scalars():
        print(f"{a.id:>3}  {a.action:<36}  {(a.payload_sha256 or '')[:16]:<16}  {a.detail}")

    session.close()


if __name__ == "__main__":
    main()
