"""The compiler is the safety boundary, so it gets the most tests."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from spike.dsl import CompileError, _RuntimeReject, compile_transform


@pytest.mark.parametrize("expr,row,expected", [
    ('sha256(request_id + ":" + user_id)[:16]',
     {"request_id": "abc", "user_id": "402"},
     "d6d2f23d5f0e91e1"),
    ('lower(replace(cid, "-", ""))', {"cid": "AB-CD"}, "abcd"),
    ('split(traceparent, "-", 1)', {"traceparent": "00-4bf9-aa-01"}, "4bf9"),
    ('regex_extract(msg, "cid=([a-f0-9]+)")', {"msg": "x cid=dead y"}, "dead"),
    ('request_id[-8:]', {"request_id": "0123456789abcdef"}, "89abcdef"),
    ('concat(a, "-", b)', {"a": "x", "b": "y"}, "x-y"),
])
def test_accepts_and_evaluates(expr, row, expected):
    assert compile_transform(expr)(row) == expected


@pytest.mark.parametrize("expr", [
    '__import__("os").system("rm -rf /")',   # the obvious one
    'open("/etc/passwd").read()',            # unknown function
    'request_id.upper()',                    # attribute access
    'eval("1+1")',
    '[c for c in request_id]',               # comprehension
    'request_id * 1000000',                  # non-Add operator
    'lambda x: x',
    'sha256(s=request_id)',                  # keyword arguments
    '"constant"',                            # references no fields
    'request_id[::2]',                       # slice step
    'request_id[n:]',                        # non-literal slice bound
    'sha256()',                              # wrong arity
    'sha256(a, b)',
    '',
])
def test_rejects(expr):
    with pytest.raises(CompileError):
        compile_transform(expr)


def test_rejects_oversized_expression():
    with pytest.raises(CompileError):
        compile_transform("a + " * 200 + "b")


def test_free_variables_are_reported():
    t = compile_transform('sha256(request_id + ":" + user_id)')
    assert t.inputs == frozenset({"request_id", "user_id"})


def test_missing_field_is_a_runtime_reject_not_a_crash():
    t = compile_transform("sha256(request_id)")
    with pytest.raises(_RuntimeReject):
        t({"user_id": "402"})


def test_unrelated_row_fields_are_ignored():
    t = compile_transform("lower(cid)")
    assert t({"cid": "AB", "noise": object()}) == "ab"
