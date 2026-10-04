"""A deliberately tiny expression language for identity transforms.

This is the COMPILE stage. An LLM proposes an expression as a string; this
module decides whether it is something we are willing to execute, and turns
it into a callable. Anything it does not explicitly recognise is rejected.

We never call eval() or exec(). The expression is parsed with `ast` and then
walked by our own interpreter, so the only operations that can ever happen
are the ones enumerated in FUNCTIONS and _eval below.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable

MAX_EXPR_CHARS = 400
MAX_NODES = 120
MAX_STR_LEN = 8192


class CompileError(ValueError):
    """The proposed expression is not something we will run."""


# --- the whitelist ----------------------------------------------------------
# Every function is pure, total, and string-in/string-out. If a transform
# cannot be written with these, we would rather reject it than widen the
# language: an unrecognised transform is a safe failure, an arbitrary one
# is not.

def _sha256(s: str) -> str: return hashlib.sha256(s.encode()).hexdigest()
def _sha1(s: str) -> str: return hashlib.sha1(s.encode()).hexdigest()
def _md5(s: str) -> str: return hashlib.md5(s.encode()).hexdigest()
def _b64encode(s: str) -> str: return base64.b64encode(s.encode()).decode()
def _b64decode(s: str) -> str: return base64.b64decode(s.encode()).decode("utf-8", "replace")
def _concat(*parts: str) -> str: return "".join(parts)


def _split(s: str, sep: str, idx: int) -> str:
    parts = s.split(sep)
    if not -len(parts) <= idx < len(parts):
        raise _RuntimeReject(f"split index {idx} out of range for {len(parts)} parts")
    return parts[idx]


def _regex_extract(s: str, pattern: str, group: int = 1) -> str:
    try:
        m = re.search(pattern, s)
    except re.error as exc:
        raise _RuntimeReject(f"bad regex: {exc}") from None
    if m is None:
        raise _RuntimeReject("regex did not match")
    try:
        return m.group(group) or ""
    except (IndexError, re.error):
        raise _RuntimeReject(f"regex has no group {group}") from None


FUNCTIONS: dict[str, tuple[Callable[..., str], int, int]] = {
    # name:        (impl,          min_args, max_args)
    "sha256":      (_sha256, 1, 1),
    "sha1":        (_sha1, 1, 1),
    "md5":         (_md5, 1, 1),
    "lower":       (str.lower, 1, 1),
    "upper":       (str.upper, 1, 1),
    "strip":       (str.strip, 1, 1),
    "reverse":     (lambda s: s[::-1], 1, 1),
    "replace":     (str.replace, 3, 3),
    "b64encode":   (_b64encode, 1, 1),
    "b64decode":   (_b64decode, 1, 1),
    "split":       (_split, 3, 3),
    "regex_extract": (_regex_extract, 2, 3),
    "concat":      (_concat, 1, 8),
}


class _RuntimeReject(Exception):
    """Raised while evaluating one row. Counts as a non-match, not a crash."""


# --- validation -------------------------------------------------------------

def _validate(node: ast.AST, free: set[str]) -> None:
    """Walk the tree, rejecting anything not explicitly permitted."""
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (str, int)) or isinstance(node.value, bool):
            raise CompileError(f"only str and int literals are allowed, got {node.value!r}")
        return

    if isinstance(node, ast.Name):
        free.add(node.id)
        return

    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, ast.Add):
            raise CompileError(f"only '+' (concatenation) is allowed, not {type(node.op).__name__}")
        _validate(node.left, free)
        _validate(node.right, free)
        return

    if isinstance(node, ast.UnaryOp):
        # Permitted purely so negative slice bounds like [-8:] work.
        if not isinstance(node.op, ast.USub) or not isinstance(node.operand, ast.Constant):
            raise CompileError("unary operators are only allowed on integer literals")
        return

    if isinstance(node, ast.Call):
        if node.keywords:
            raise CompileError("keyword arguments are not supported; pass arguments positionally")
        if not isinstance(node.func, ast.Name):
            raise CompileError("method calls and attribute access are not allowed")
        name = node.func.id
        if name not in FUNCTIONS:
            raise CompileError(
                f"unknown function {name!r}. Allowed: {', '.join(sorted(FUNCTIONS))}")
        _, lo, hi = FUNCTIONS[name]
        if not lo <= len(node.args) <= hi:
            raise CompileError(
                f"{name}() takes {lo}..{hi} arguments, got {len(node.args)}")
        for arg in node.args:
            _validate(arg, free)
        return

    if isinstance(node, ast.Subscript):
        _validate(node.value, free)
        _validate(node.slice, free)
        return

    if isinstance(node, ast.Slice):
        if node.step is not None:
            raise CompileError("slice steps are not supported")
        for part in (node.lower, node.upper):
            if part is not None:
                _validate_int(part)
        return

    raise CompileError(f"{type(node).__name__} is not allowed in a transform expression")


def _validate_int(node: ast.AST) -> None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return _validate_int(node.operand)
    raise CompileError("slice bounds must be integer literals")


# --- evaluation -------------------------------------------------------------

def _eval(node: ast.AST, env: dict[str, str]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id not in env:
            raise _RuntimeReject(f"field {node.id!r} is missing from this row")
        return env[node.id]

    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, env), _eval(node.right, env)
        if not isinstance(left, str) or not isinstance(right, str):
            raise _RuntimeReject("'+' only concatenates strings here")
        out = left + right
        if len(out) > MAX_STR_LEN:
            raise _RuntimeReject("intermediate string too long")
        return out

    if isinstance(node, ast.UnaryOp):
        return -_eval(node.operand, env)

    if isinstance(node, ast.Call):
        fn, _, _ = FUNCTIONS[node.func.id]
        args = [_eval(a, env) for a in node.args]
        try:
            return fn(*args)
        except _RuntimeReject:
            raise
        except Exception as exc:
            raise _RuntimeReject(f"{node.func.id}() failed: {exc}") from None

    if isinstance(node, ast.Subscript):
        value = _eval(node.value, env)
        if not isinstance(value, str):
            raise _RuntimeReject("only strings can be sliced")
        if isinstance(node.slice, ast.Slice):
            lo = _eval(node.slice.lower, env) if node.slice.lower else None
            hi = _eval(node.slice.upper, env) if node.slice.upper else None
            return value[lo:hi]
        idx = _eval(node.slice, env)
        if not isinstance(idx, int):
            raise _RuntimeReject("string indices must be integers")
        if not -len(value) <= idx < len(value):
            raise _RuntimeReject(f"index {idx} out of range")
        return value[idx]

    raise _RuntimeReject(f"unexpected node {type(node).__name__}")


# --- public surface ---------------------------------------------------------

@dataclass(frozen=True)
class Transform:
    """A compiled, executable identity transform."""

    expression: str
    inputs: frozenset[str]
    _tree: ast.Expression

    def __call__(self, row: dict[str, Any]) -> str:
        env = {k: str(v) for k, v in row.items() if k in self.inputs}
        out = _eval(self._tree.body, env)
        if not isinstance(out, str):
            raise _RuntimeReject(f"transform produced {type(out).__name__}, expected str")
        return out


def compile_transform(expression: str) -> Transform:
    """Parse and validate `expression`, or raise CompileError.

    A CompileError is a *good* outcome when the proposal was nonsense - it
    means the bad idea never reached any data.
    """
    if not expression or not expression.strip():
        raise CompileError("empty expression")
    if len(expression) > MAX_EXPR_CHARS:
        raise CompileError(f"expression longer than {MAX_EXPR_CHARS} characters")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise CompileError(f"not a parseable expression: {exc.msg}") from None

    if len(list(ast.walk(tree))) > MAX_NODES:
        raise CompileError(f"expression has more than {MAX_NODES} nodes")

    free: set[str] = set()
    _validate(tree.body, free)

    if not free:
        raise CompileError(
            "expression references no input fields, so it is a constant - rejected")

    return Transform(expression=expression.strip(), inputs=frozenset(free), _tree=tree)


GRAMMAR_DOC = """\
An expression over the source row's fields. Field names are bare identifiers.

  functions : sha256 sha1 md5 lower upper strip reverse
              replace(s, old, new)  split(s, sep, index)
              regex_extract(s, pattern[, group])  b64encode  b64decode
              concat(a, b, ...)
  operators : +  (string concatenation)
              [a:b] [a:] [:b] [i]  (slicing / indexing, integer literals only)
  literals  : "text"  and integers

Everything is a string. Anything else - method calls, attribute access,
imports, arithmetic, comprehensions, keyword arguments - is rejected.

  examples: sha256(request_id + ":" + user_id)[:16]
            lower(replace(corr_id, "-", ""))
            split(traceparent, "-", 1)
            regex_extract(msg, "cid=([a-f0-9]+)")
"""
