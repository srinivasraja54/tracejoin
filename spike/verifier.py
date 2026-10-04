"""Join replay. This is the VERIFY stage, and the point of the whole spike.

A proposed transform is a hypothesis about how identity crosses a boundary.
We test it the only way that means anything: take real records from the
source side, apply the transform, and see whether the results actually exist
on the target side.

No LLM is involved here. That is deliberate - the model gets to be creative
about *what* the mapping might be, and this module decides whether it is
*true*.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .dsl import Transform, _RuntimeReject

VERIFIED_THRESHOLD = 0.95
PROBABLE_THRESHOLD = 0.50
# A transform that maps many distinct inputs onto few outputs can score a high
# join_yield by accident. Real identity mappings are injective.
INJECTIVITY_THRESHOLD = 0.90


@dataclass
class VerificationResult:
    verdict: str                 # verified | probable | rejected | degenerate | no_data
    sampled: int = 0
    matched: int = 0
    join_yield: float = 0.0
    distinct_inputs: int = 0
    distinct_outputs: int = 0
    runtime_rejects: int = 0
    note: str = ""
    failures: list[dict] = field(default_factory=list)
    target_samples: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict == "verified"


def build_target_index(rows: list[dict], field_name: str) -> set[str]:
    """Every value the target side actually holds for `field_name`."""
    return {str(r[field_name]) for r in rows if field_name in r}


def verify(
    transform: Transform,
    source_rows: list[dict],
    target_values: set[str],
    *,
    sample_size: int = 200,
    seed: int = 7,
    max_failure_examples: int = 3,
) -> VerificationResult:
    """Replay `transform` from source rows into the target key space."""
    required = set(transform.inputs)
    usable = [r for r in source_rows if required.issubset(r.keys())]

    if not usable:
        present = sorted({k for r in source_rows[:50] for k in r})
        return VerificationResult(
            verdict="no_data",
            note=(f"no source row contains all of {sorted(required)}. "
                  f"Fields actually present: {present}"),
            target_samples=sorted(target_values)[:5],
        )

    rng = random.Random(seed)
    sample = usable if len(usable) <= sample_size else rng.sample(usable, sample_size)

    matched = 0
    rejects = 0
    seen_inputs: set[tuple] = set()
    seen_outputs: set[str] = set()
    failures: list[dict] = []

    for row in sample:
        inputs = {k: str(row[k]) for k in sorted(required)}
        seen_inputs.add(tuple(inputs.items()))
        try:
            produced = transform(row)
        except _RuntimeReject as exc:
            rejects += 1
            if len(failures) < max_failure_examples:
                failures.append({"inputs": inputs, "produced": None, "error": str(exc)})
            continue

        seen_outputs.add(produced)
        if produced in target_values:
            matched += 1
        elif len(failures) < max_failure_examples:
            failures.append({"inputs": inputs, "produced": produced, "error": None})

    n = len(sample)
    join_yield = matched / n if n else 0.0
    result = VerificationResult(
        sampled=n,
        matched=matched,
        join_yield=join_yield,
        distinct_inputs=len(seen_inputs),
        distinct_outputs=len(seen_outputs),
        runtime_rejects=rejects,
        failures=failures,
        target_samples=sorted(target_values)[:5],
        verdict="rejected",
    )

    # Degeneracy check runs before the yield check on purpose: a collapsing
    # transform that happens to score well is worse than one that scores badly,
    # because it looks like success.
    if seen_inputs and len(seen_outputs) / len(seen_inputs) < INJECTIVITY_THRESHOLD:
        result.verdict = "degenerate"
        result.note = (
            f"{len(seen_inputs)} distinct inputs collapsed to {len(seen_outputs)} "
            "distinct outputs - an identity mapping should be injective")
        return result

    if join_yield >= VERIFIED_THRESHOLD:
        result.verdict = "verified"
    elif join_yield >= PROBABLE_THRESHOLD:
        result.verdict = "probable"
        result.note = ("joins for most but not all sampled records - likely "
                       "correct with an edge case, or correct with data loss")
    else:
        result.note = "the produced values do not exist on the target side"

    return result
