"""Plain dataclasses shared by every stage.

Deliberately stdlib-only so the offline path (`--proposer stub`) runs and the
tests pass with nothing installed. Pydantic is imported lazily, and only by
the Anthropic proposer that actually needs it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Evidence:
    file: str
    lines: str
    quote: str


@dataclass
class Proposal:
    """One hypothesis about how identity crosses a boundary."""

    kind: str                      # identity | transform | bridge
    expression: str
    inputs: list[str]
    target_field: str
    reasoning: str
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Attempt:
    n: int
    proposal: Proposal | None
    stage: str                     # propose | compile | verify
    outcome: str
    detail: str = ""
    join_yield: float | None = None


@dataclass
class Boundary:
    """The one edge this spike is trying to resolve."""

    edge_id: str
    source_hop: str
    target_hop: str
    target_field: str
    source_fields: list[str]
    source_samples: list[dict[str, Any]]
    target_samples: list[str]
    code: dict[str, str]           # path -> contents


@dataclass
class SpikeResult:
    resolved: bool
    attempts: list[Attempt]
    proposal: Proposal | None = None
    join_yield: float | None = None
    sampled: int = 0
    matched: int = 0

    def to_dict(self) -> dict:
        return {
            "resolved": self.resolved,
            "join_yield": self.join_yield,
            "sampled": self.sampled,
            "matched": self.matched,
            "attempts": [asdict(a) for a in self.attempts],
            "proposal": asdict(self.proposal) if self.proposal else None,
        }
