"""PROPOSE -> COMPILE -> VERIFY -> PERSIST.

The whole thesis of the project is in this file, and it is short on purpose.
The only thing worth noticing: a failed verification is not the end of the
run. It is turned into evidence and handed back to the proposer, so the next
proposal is informed by data rather than by a second guess.
"""

from __future__ import annotations

from dataclasses import asdict

from .dsl import CompileError, compile_transform
from .models import Attempt, Boundary, Proposal, SpikeResult
from .verifier import VerificationResult, verify


def run_spike(
    proposer,
    boundary: Boundary,
    source_rows: list[dict],
    target_values: set[str],
    *,
    max_attempts: int = 3,
    sample_size: int = 200,
    on_event=lambda *_: None,
) -> SpikeResult:
    attempts: list[Attempt] = []
    failures: list[dict] = []

    for n in range(1, max_attempts + 1):
        on_event("attempt", n)

        # 1. PROPOSE -------------------------------------------------------
        proposal: Proposal = proposer.propose(boundary, failures)
        on_event("proposal", proposal)

        # 2. COMPILE -------------------------------------------------------
        try:
            transform = compile_transform(proposal.expression)
        except CompileError as exc:
            attempts.append(Attempt(n, proposal, "compile", "rejected", str(exc)))
            failures.append({"expression": proposal.expression,
                             "outcome": "rejected at compile",
                             "detail": str(exc)})
            on_event("compile_failed", str(exc))
            continue
        on_event("compiled", transform)

        # 3. VERIFY --------------------------------------------------------
        result: VerificationResult = verify(
            transform, source_rows, target_values, sample_size=sample_size)
        on_event("verified", result)

        attempts.append(Attempt(n, proposal, "verify", result.verdict,
                                result.note, result.join_yield))

        if result.ok:
            return SpikeResult(
                resolved=True, attempts=attempts, proposal=proposal,
                join_yield=result.join_yield,
                sampled=result.sampled, matched=result.matched)

        # 4. The failure becomes evidence for the next proposal.
        failures.append({
            "expression": proposal.expression,
            "outcome": result.verdict,
            "detail": result.note,
            "join_yield": result.join_yield,
            "matched": result.matched,
            "sampled": result.sampled,
            "examples": result.failures,
        })

    return SpikeResult(resolved=False, attempts=attempts)


def to_correlation_map(boundary: Boundary, outcome: SpikeResult) -> dict:
    """The persisted artifact. M0 writes a single edge; M1 grows this."""
    if not outcome.resolved or outcome.proposal is None:
        return {"edges": [], "unresolved": [{
            "edge_id": boundary.edge_id,
            "reason": "no proposal passed verification",
            "attempts": [asdict(a) for a in outcome.attempts],
        }]}

    p = outcome.proposal
    return {
        "map_version": "m0",
        "edges": [{
            "id": boundary.edge_id,
            "kind": p.kind,
            "from": {"hop": boundary.source_hop, "fields": p.inputs},
            "to": {"hop": boundary.target_hop, "field": p.target_field},
            "transform": {"expr": p.expression},
            "evidence": [asdict(e) for e in p.evidence],
            "confidence": "verified",
            "verification": {
                "method": "join_replay",
                "sampled": outcome.sampled,
                "matched": outcome.matched,
                "join_yield": round(outcome.join_yield or 0.0, 4),
            },
        }],
    }
