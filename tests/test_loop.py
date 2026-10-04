"""End-to-end, against the generated dataset, with no network."""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from spike.loop import run_spike, to_correlation_map
from spike.models import Boundary, Evidence, Proposal
from spike.proposer import StubProposer, render_prompt
from spike.verifier import build_target_index

DATA = pathlib.Path(__file__).resolve().parents[1] / "data"


def _load():
    src = [json.loads(l) for l in (DATA / "middleware.jsonl").read_text().splitlines()]
    tgt = [json.loads(l) for l in (DATA / "appdb.jsonl").read_text().splitlines()]
    return src, build_target_index(tgt, "internal_tx_hash")


def _boundary(src, tgt):
    with_ids = [r for r in src if "request_id" in r]
    return Boundary(
        edge_id="edge_test", source_hop="mw", target_hop="db",
        target_field="internal_tx_hash",
        source_fields=sorted(with_ids[0]), source_samples=with_ids[:2],
        target_samples=sorted(tgt)[:3], code={"x.py": "..."})


def test_stub_converges_on_second_attempt():
    src, tgt = _load()
    out = run_spike(StubProposer(), _boundary(src, tgt), src, tgt)
    assert out.resolved
    assert len(out.attempts) == 2                 # missed once, then corrected
    assert out.attempts[0].outcome == "rejected"
    assert out.join_yield >= 0.95


def test_recovered_expression_matches_ground_truth():
    src, tgt = _load()
    truth = json.loads((DATA / "ground_truth.json").read_text())
    out = run_spike(StubProposer(), _boundary(src, tgt), src, tgt)
    assert out.proposal.expression == truth["expression"]


def test_unresolvable_boundary_is_reported_not_guessed():
    class Stubborn:
        def propose(self, boundary, failures):
            return Proposal(kind="transform", expression="sha256(request_id)",
                            inputs=["request_id"], target_field="internal_tx_hash",
                            reasoning="wrong on purpose", evidence=[])

    src, tgt = _load()
    out = run_spike(Stubborn(), _boundary(src, tgt), src, tgt, max_attempts=2)
    assert not out.resolved and out.proposal is None
    assert to_correlation_map(_boundary(src, tgt), out)["edges"] == []


def test_compile_failure_is_fed_back_as_evidence():
    seen = []

    class Bad:
        def __init__(self): self.n = 0
        def propose(self, boundary, failures):
            seen.append(list(failures))
            self.n += 1
            expr = "request_id.upper()" if self.n == 1 else \
                'sha256(request_id + ":" + user_id)[:16]'
            return Proposal(kind="transform", expression=expr,
                            inputs=["request_id", "user_id"],
                            target_field="internal_tx_hash", reasoning="", evidence=[])

    src, tgt = _load()
    out = run_spike(Bad(), _boundary(src, tgt), src, tgt)
    assert out.resolved
    assert seen[1] and "compile" in seen[1][0]["outcome"]


def test_prompt_includes_failure_evidence():
    src, tgt = _load()
    prompt = render_prompt(_boundary(src, tgt), [{
        "expression": "sha256(request_id)", "outcome": "rejected",
        "join_yield": 0.0, "matched": 0, "sampled": 200,
        "examples": [{"inputs": {"request_id": "x"}, "produced": "y", "error": None}]}])
    assert "join_yield: 0.00" in prompt and "not present on the target side" in prompt


def test_correlation_map_records_provenance():
    src, tgt = _load()
    b = _boundary(src, tgt)
    out = run_spike(StubProposer(), b, src, tgt)
    edge = to_correlation_map(b, out)["edges"][0]
    assert edge["kind"] == "transform"
    assert edge["verification"]["method"] == "join_replay"
    assert edge["evidence"][0]["file"].endswith("payment_service.py")
