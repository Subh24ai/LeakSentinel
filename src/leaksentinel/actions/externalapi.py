"""A MOCKED external claims/CRM API — deliberately in its own file so it is
*obviously swappable* for a real insurer or CRM client later.

In production this becomes a thin HTTP client (insurer claims portal, a CRM's
REST API, a message queue, ...). Nothing else in the actions layer should know
or care which: callers depend only on the two method shapes below. The stub
records every call on ``self.calls`` so tests and the demo can introspect what
*would* have been sent over the wire.

Note: this stub is intentionally dumb. The real idempotency / no-double-pay
guarantee lives in :mod:`leaksentinel.actions.remediation` (our own DB), not
here — we never trust an external system to dedupe for us.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class ExternalCall:
    """One recorded call made against the stub (audit/inspection aid)."""

    method: str
    policy_no: str
    reason_code: str
    idempotency_key: str
    amount: Decimal | None = None


class ExternalClaimsAPI:
    """Local stand-in for a real insurer/CRM claims API.

    Swap this for a real client by keeping the same method signatures:
    ``submit_claim`` and ``request_rebilling``. Each returns a small dict with
    an external reference the caller can persist for traceability.
    """

    def __init__(self) -> None:
        self.calls: list[ExternalCall] = []

    def submit_claim(
        self,
        *,
        policy_no: str,
        amount: Decimal,
        reason_code: str,
        idempotency_key: str,
    ) -> dict:
        """Pretend to lodge a shortfall claim with the insurer."""
        self.calls.append(
            ExternalCall(
                method="submit_claim",
                policy_no=policy_no,
                reason_code=reason_code,
                idempotency_key=idempotency_key,
                amount=amount,
            )
        )
        # A real client would POST and get back the insurer's claim id.
        return {
            "accepted": True,
            "external_ref": f"INS-CLAIM-{idempotency_key[:12]}",
        }

    def request_rebilling(
        self,
        *,
        policy_no: str,
        reason_code: str,
        idempotency_key: str,
    ) -> dict:
        """Pretend to ask the billing system to re-issue a bill."""
        self.calls.append(
            ExternalCall(
                method="request_rebilling",
                policy_no=policy_no,
                reason_code=reason_code,
                idempotency_key=idempotency_key,
            )
        )
        return {
            "accepted": True,
            "external_ref": f"REBILL-{idempotency_key[:12]}",
        }
