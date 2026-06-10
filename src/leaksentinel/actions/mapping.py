"""Map a detector :class:`Finding` + its reconciliation row into the
:class:`ActionableFinding` that the actions layer (:func:`route`) consumes.

This is the seam between *detection* (which says "policy X looks wrong, here's
why") and *action* (which needs a concrete, gateable claim: a policy, a reason
code, an amount, and a confirmed status). Keeping the mapping in one tested
function means the gate's contract never has to reach back into the database.

How the claimable ``amount`` is derived from the reconciliation row:

  * MISSING_COMMISSION — nothing was paid, so the whole expected commission is
    at risk. ``delta = actual(0) - expected``, hence ``abs(delta) == expected``.
  * UNDERPAID          — the shortfall, ``abs(delta) = expected - actual``.
  * DUPLICATE / OVERPAID — the overpayment / clawback exposure, ``abs(delta)``.
  * Anything matched (delta 0/None) — fall back to ``expected`` as the value at
    stake (e.g. a 1+1 renewal owed on an otherwise correctly-paid policy).

The ``status`` is the reconciliation row's ``resolution_state`` (``open`` for a
confirmed leak). A matched policy carries no resolution state, so it maps to an
empty status and the gate will (correctly) refuse to auto-act on it — sending it
to a human via escalation instead.
"""

from __future__ import annotations

from decimal import Decimal

from leaksentinel.actions.remediation import ActionableFinding, _money
from leaksentinel.detection.rules import Finding
from leaksentinel.reconciliation.models import ReconciliationResult


def build_actionable_finding(
    finding: Finding,
    recon: ReconciliationResult | None,
) -> ActionableFinding:
    """Combine a detector finding with its reconciliation row into the gateable
    :class:`ActionableFinding` consumed by :func:`leaksentinel.actions.escalation.route`.
    """
    if recon is None:
        amount = Decimal("0.00")
        status = ""
    else:
        expected = recon.expected or Decimal("0.00")
        delta = recon.delta
        amount = abs(delta) if (delta is not None and delta != 0) else expected
        status = recon.resolution_state or ""

    return ActionableFinding(
        policy_no=finding.policy_no,
        reason_code=finding.reason_code,
        amount=_money(amount),
        status=status,
        detail=finding.explanation,
    )
