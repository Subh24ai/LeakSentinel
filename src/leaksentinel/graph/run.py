"""CLI entry point: run the whole LeakSentinel pipeline end-to-end.

    python -m leaksentinel.graph.run        (or: make pipeline)

Runs Intake → Reconcile → Detect → Decide → Remediate/Escalate → Finalize over
the synthetic data in Postgres, wires up LangSmith tracing if configured, and
prints a final summary: policies processed, leaks by reason code, auto-remediated
vs. escalated counts, total ₹ at risk, and total ₹ claimed back.
"""

from __future__ import annotations

import logging

from leaksentinel.graph.state import ReconciliationState
from leaksentinel.graph.tracing import configure_tracing
from leaksentinel.graph.workflow import build_graph


def run_pipeline(reset_actions: bool = True) -> ReconciliationState:
    """Execute the graph once and return the final typed state."""
    configure_tracing()
    graph = build_graph()
    final = graph.invoke(ReconciliationState(reset_actions=reset_actions))
    # LangGraph returns the merged state as a dict; re-validate to the model.
    return ReconciliationState.model_validate(final)


def _print_summary(state: ReconciliationState) -> None:
    s = state.summary
    print("\n" + "=" * 60)
    print("  LeakSentinel — end-to-end pipeline summary")
    print("=" * 60)
    print(f"  Policies processed       : {s['policies_processed']}")

    print("\n  Leaks detected by reason_code:")
    if s["leaks_by_reason"]:
        for reason, count in s["leaks_by_reason"].items():
            print(f"    {reason:<34} {count:>4}")
    else:
        print("    (none)")
    print(f"    {'(informational / rounding)':<34} {s['informational']:>4}")

    print("\n  Disposition:")
    print(f"    Auto-remediated          : {s['auto_remediated']}")
    print(f"    Escalated to human queue : {s['escalated']}")
    print(f"    Below claim threshold    : {s['below_threshold']}")

    print("\n  Money:")
    print(f"    Underpayment exposure    : ₹{s['underpayment_exposure']}  (owed to us)")
    print(f"    Clawback liability       : ₹{s['clawback_exposure']}  (we owe back)")
    print(f"    Total claimed (lodged)   : ₹{s['total_claimed']}")
    print("=" * 60)

    # Conservation check, printed so the run is self-auditing.
    accounted = s["auto_remediated"] + s["escalated"] + s["below_threshold"]
    n_plan = len(state.plan)
    ok = accounted == n_plan
    print(f"  Conservation: {accounted}/{n_plan} actionable findings accounted "
          f"for — {'OK' if ok else 'MISMATCH'}.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    state = run_pipeline()
    _print_summary(state)


if __name__ == "__main__":
    main()
