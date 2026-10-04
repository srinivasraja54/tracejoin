"""Verification is what makes a proposal falsifiable."""

import hashlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from spike.dsl import compile_transform
from spike.verifier import build_target_index, verify

GOOD = 'sha256(request_id + ":" + user_id)[:16]'


def _rows(n=50):
    return [{"request_id": f"rq{i:04d}", "user_id": str(100 + i)} for i in range(n)]


def _truth(rows):
    return {hashlib.sha256(f"{r['request_id']}:{r['user_id']}".encode())
            .hexdigest()[:16] for r in rows}


def test_correct_transform_verifies():
    rows = _rows()
    r = verify(compile_transform(GOOD), rows, _truth(rows))
    assert r.verdict == "verified" and r.join_yield == 1.0 and r.ok


def test_near_miss_is_rejected():
    rows = _rows()
    r = verify(compile_transform('sha256(request_id + user_id)[:16]'), rows, _truth(rows))
    assert r.verdict == "rejected" and r.join_yield == 0.0
    assert r.failures and r.failures[0]["produced"]


def test_partial_data_loss_reads_as_probable_not_verified():
    rows = _rows(100)
    partial = _truth(rows[:70])
    r = verify(compile_transform(GOOD), rows, partial)
    assert r.verdict == "probable"


def test_degenerate_transform_is_caught_even_when_it_joins():
    # Collapses every row onto one value that does exist on the target side.
    rows = _rows()
    target = {"x"} | _truth(rows)
    r = verify(compile_transform('replace(user_id, user_id, "x")'), rows, target)
    assert r.verdict == "degenerate"
    assert r.join_yield == 1.0          # it "worked" - and is still rejected


def test_missing_source_fields_reports_no_data():
    r = verify(compile_transform("sha256(corr_id)"), _rows(), {"whatever"})
    assert r.verdict == "no_data" and "corr_id" in r.note


def test_target_index_ignores_rows_without_the_field():
    idx = build_target_index([{"internal_tx_hash": "a"}, {"event": "noise"}],
                             "internal_tx_hash")
    assert idx == {"a"}
