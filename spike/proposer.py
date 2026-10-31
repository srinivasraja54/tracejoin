"""The PROPOSE stage - the only place an LLM is allowed to have an opinion.

Two implementations:
  AnthropicProposer  real Claude, reads the source code, proposes a transform
  StubProposer       scripted; needs no API key. Its first answer is the
                     realistic near-miss (forgetting the ":" separator) so the
                     retry-on-failure-evidence path is exercised offline.
"""

from __future__ import annotations

import json
import textwrap
from typing import Protocol

from .dsl import GRAMMAR_DOC
from .models import Boundary, Evidence, Proposal

SYSTEM_PROMPT = """\
You recover how a request's identity is transformed as it crosses a boundary
between two systems that log to different stores.

You are given the source code that sits at the boundary, the field names and
sample rows the source system logs, and sample values of the single field the
target system logs. Your job is to work out the expression that turns source
fields into the target field's value.

You must return an expression in this restricted language:

{grammar}

Rules:
- The expression must use only fields that appear in the source rows.
- Base the expression on what the code actually does, not on what would be
  conventional. Separators, truncation lengths and field order matter.
- Quote the exact line(s) you relied on in `evidence`. If you cannot find
  code that explains the target field, say so in `reasoning` and return
  kind="bridge" with your best guess - do not invent a plausible-looking
  transform to fill the gap.
- Code at a boundary usually contains several hash-like helpers. Only one of
  them produces the target field. Check what each one is used for.
"""

USER_TEMPLATE = """\
## Boundary
{source_hop} -> {target_hop}

## Target field to reproduce
`{target_field}` on {target_hop}

Sample values it holds:
{target_samples}

## Source rows available on {source_hop}
Fields: {source_fields}

Sample rows:
{source_samples}

## Source code at the boundary
{code}
{failures}"""

FAILURE_TEMPLATE = """\

## Previous attempts (all rejected - do not repeat them)
{blocks}
Use these results as evidence. A join_yield of 0.00 means the values you
produced do not exist on the target side at all, so the transform is wrong -
not merely imprecise."""


# USD per million tokens, Claude API list price.
PRICING = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5":   (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    if model not in PRICING:
        return None
    pin, pout = PRICING[model]
    return input_tokens / 1e6 * pin + output_tokens / 1e6 * pout


class Proposer(Protocol):
    def propose(self, boundary: Boundary, failures: list[dict]) -> Proposal: ...


def render_prompt(boundary: Boundary, failures: list[dict]) -> str:
    code = "\n\n".join(
        f"### {path}\n```python\n{body}\n```" for path, body in boundary.code.items())
    failure_text = ""
    if failures:
        blocks = []
        for f in failures:
            lines = [f"\n- expression: `{f['expression']}`",
                     f"  outcome: {f['outcome']}"]
            if f.get("join_yield") is not None:
                lines.append(f"  join_yield: {f['join_yield']:.2f} "
                             f"({f.get('matched', 0)}/{f.get('sampled', 0)} matched)")
            if f.get("detail"):
                lines.append(f"  detail: {f['detail']}")
            for ex in f.get("examples", [])[:3]:
                if ex.get("produced") is not None:
                    lines.append(f"  {ex['inputs']} produced '{ex['produced']}' "
                                 "which is not present on the target side")
                else:
                    lines.append(f"  {ex['inputs']} could not be evaluated: {ex['error']}")
            blocks.append("\n".join(lines))
        failure_text = FAILURE_TEMPLATE.format(blocks="\n".join(blocks))

    return USER_TEMPLATE.format(
        source_hop=boundary.source_hop,
        target_hop=boundary.target_hop,
        target_field=boundary.target_field,
        target_samples="\n".join(f"  {v}" for v in boundary.target_samples),
        source_fields=", ".join(boundary.source_fields),
        source_samples="\n".join(f"  {json.dumps(r)}" for r in boundary.source_samples),
        code=code,
        failures=failure_text,
    )


class AnthropicProposer:
    """Calls Claude and forces a schema-valid answer via structured outputs."""

    def __init__(self, model: str = "claude-opus-5", max_tokens: int = 16000):
        import anthropic  # imported here so the offline path needs no install

        self.client = anthropic.Anthropic()
        self.model = model
        self.max_tokens = max_tokens
        self._schema = _proposal_schema()
        self.last_usage = None
        # Cost is a first-class result, not an afterthought: a run that can't
        # report what it cost can't be compared against anything.
        self.input_tokens = 0
        self.output_tokens = 0

    def propose(self, boundary: Boundary, failures: list[dict]) -> Proposal:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT.format(grammar=textwrap.indent(GRAMMAR_DOC, "  ")),
            messages=[{"role": "user", "content": render_prompt(boundary, failures)}],
            output_format=self._schema,
        )
        self.last_usage = response.usage
        self.input_tokens += getattr(response.usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(response.usage, "output_tokens", 0) or 0
        parsed = response.parsed_output
        return Proposal(
            kind=parsed.kind,
            expression=parsed.expression,
            inputs=list(parsed.inputs),
            target_field=parsed.target_field,
            reasoning=parsed.reasoning,
            evidence=[Evidence(file=e.file, lines=e.lines, quote=e.quote)
                      for e in parsed.evidence],
        )


def _proposal_schema():
    """Pydantic mirror of Proposal, built lazily so stdlib-only runs work."""
    from typing import List, Literal

    from pydantic import BaseModel, Field

    class _Evidence(BaseModel):
        file: str = Field(description="Path of the file the claim rests on")
        lines: str = Field(description="Line range, e.g. '41-58'")
        quote: str = Field(description="The exact line(s), copied verbatim")

    class _Proposal(BaseModel):
        kind: Literal["identity", "transform", "bridge"]
        expression: str = Field(description="Expression in the restricted language")
        inputs: List[str] = Field(description="Source fields the expression reads")
        target_field: str
        reasoning: str = Field(description="Two sentences at most")
        evidence: List[_Evidence]

    return _Proposal


class StubProposer:
    """Offline stand-in. Scripted to miss first, then correct itself."""

    SCRIPT = [
        Proposal(
            kind="transform",
            expression='sha256(request_id + user_id)[:16]',
            inputs=["request_id", "user_id"],
            target_field="internal_tx_hash",
            reasoning=("_tx_hash digests the request id together with the user id "
                       "and truncates to TX_HASH_LEN."),
            evidence=[Evidence(
                file="sample_app/payment_service.py", lines="56-58",
                quote='digest = hashlib.sha256(f"{req.request_id}:{req.user_id}".encode()).hexdigest()')],
        ),
        Proposal(
            kind="transform",
            expression='sha256(request_id + ":" + user_id)[:16]',
            inputs=["request_id", "user_id"],
            target_field="internal_tx_hash",
            reasoning=("The f-string in _tx_hash puts a literal ':' between the two "
                       "values; my first attempt concatenated them directly."),
            evidence=[Evidence(
                file="sample_app/payment_service.py", lines="56-58",
                quote='digest = hashlib.sha256(f"{req.request_id}:{req.user_id}".encode()).hexdigest()')],
        ),
    ]

    def __init__(self):
        self.calls = 0
        self.last_usage = None
        self.input_tokens = 0
        self.output_tokens = 0

    def propose(self, boundary: Boundary, failures: list[dict]) -> Proposal:
        proposal = self.SCRIPT[min(self.calls, len(self.SCRIPT) - 1)]
        self.calls += 1
        return proposal
