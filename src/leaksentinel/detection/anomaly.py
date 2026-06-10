"""Isolation Forest novelty detector (the SECONDARY detection layer).

This exists to catch leakage *patterns the rules don't yet encode* — a feed row
whose commission/premium shape is statistically unlike everything else, but
which no deterministic rule flagged. It is deliberately subordinate to
:mod:`leaksentinel.detection.rules`:

* Finance acts on the rules. Each rule flag has a defensible reason code and a
  number a human can re-derive and put in a dispute. An anomaly score cannot be
  disputed with an insurer ("the model returned -0.61" is not an argument), so
  ML never overrides or silences a rule.
* The model only reports on policies the rules did NOT already cover (pass them
  via ``exclude``), and labels every hit ``ANOMALY_UNCLASSIFIED`` — explicitly
  "we don't know what this is, a human should look." It surfaces candidates for
  a *future* rule, not final verdicts.

Features are built from the normalized insurer feed rows: the paid commission,
the premium, and the implied commission rate (commission / premium). Outliers in
that space are unusual payment shapes.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
from sklearn.ensemble import IsolationForest
from sqlalchemy import select

from leaksentinel.detection.rules import DetectionReason, Finding, Severity
from leaksentinel.reconciliation.models import InsurerCommissionFeed

# Minimum rows before fitting is meaningful.
_MIN_ROWS = 20


def _feature_rows(session) -> tuple[list[str], np.ndarray]:
    """Return (policy_nos, feature matrix) from normalized feed rows.

    Feature vector per feed row: [commission_amount, premium, implied_rate].
    """
    rows = session.execute(
        select(
            InsurerCommissionFeed.policy_no,
            InsurerCommissionFeed.commission_amount,
            InsurerCommissionFeed.premium,
        )
        .where(InsurerCommissionFeed.policy_no.is_not(None))
        .where(InsurerCommissionFeed.commission_amount.is_not(None))
        .where(InsurerCommissionFeed.premium.is_not(None))
    ).all()

    policy_nos: list[str] = []
    features: list[list[float]] = []
    for policy_no, commission, premium in rows:
        commission_f = float(commission)
        premium_f = float(premium)
        implied_rate = commission_f / premium_f if premium_f else 0.0
        policy_nos.append(policy_no)
        features.append([commission_f, premium_f, implied_rate])

    return policy_nos, np.array(features, dtype=float)


def run_anomaly(
    session,
    exclude: Iterable[str] | None = None,
    contamination: float = 0.02,
    random_state: int = 42,
) -> list[Finding]:
    """Flag novel outlier feed rows the rules did not catch.

    ``exclude`` is the set of policy_nos already covered by rules; those are
    removed from the output so the ML layer only reports genuine novelty.
    """
    excluded = set(exclude or ())
    policy_nos, X = _feature_rows(session)
    if len(X) < _MIN_ROWS:
        return []

    model = IsolationForest(
        contamination=contamination,
        random_state=random_state,
        n_estimators=200,
    )
    labels = model.fit_predict(X)           # -1 = outlier, 1 = inlier
    scores = model.score_samples(X)         # lower = more anomalous

    # Keep the most anomalous score per policy, for outliers not covered by rules.
    best: dict[str, float] = {}
    for policy_no, label, score in zip(policy_nos, labels, scores, strict=True):
        if label != -1 or policy_no in excluded:
            continue
        if policy_no not in best or score < best[policy_no]:
            best[policy_no] = float(score)

    return [
        Finding(
            policy_no=policy_no,
            reason_code=DetectionReason.ANOMALY_UNCLASSIFIED.value,
            severity=Severity.LOW.value,
            explanation=(
                f"Policy {policy_no} is a statistical outlier in commission/premium "
                f"space (isolation-forest score {score:.3f}) not matched by any rule; "
                f"flagged for human review."
            ),
            detector="anomaly:isolation_forest",
            score=score,
        )
        for policy_no, score in sorted(best.items(), key=lambda kv: kv[1])
    ]
