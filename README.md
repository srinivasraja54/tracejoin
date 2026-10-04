# tracejoin

**Recovering join keys between systems that share no common identifier — by reading the
source code, and proving the answer against real data before trusting it.**

When a request crosses several systems, its identity rarely survives intact. It gets renamed,
truncated, re-encoded — and sometimes put through a one-way function. Distributed tracing assumes
the identifier is preserved. When it isn't, the trail goes cold, and the relationship exists in
exactly one place: the source code.

This repo is a focused spike that tests one question, in about 600 lines:

> **Can a language model read source code and recover a join key that no telemetry could reveal —
> and can we prove it got it right?**

```
$ python run.py --proposer stub

── attempt 1 ──────────────────────────────────────────
  PROPOSE  sha256(request_id + user_id)[:16]
  COMPILE  ok  inputs: ['request_id', 'user_id']
  VERIFY   rejected  join_yield=0.000 (0/200)
           {'request_id': 'f59fb16e…', 'user_id': '271'} -> 3f57db01fb1bfd57 (absent on target)

── attempt 2 ──────────────────────────────────────────
  PROPOSE  sha256(request_id + ":" + user_id)[:16]
           The f-string puts a literal ':' between the two values;
           my first attempt concatenated them directly.
  COMPILE  ok  inputs: ['request_id', 'user_id']
  VERIFY   verified  join_yield=0.975 (195/200)

RESOLVED after 2 attempt(s)
```

**Read that output again, because it is the whole idea in miniature.** The model was wrong the
first time — in a realistic way, missing a separator inside an f-string. It was not *caught* being
wrong by a human. It was caught by data, and the failure was handed back to it as evidence, and
the second answer was right.

There is no agent framework here, no Docker, no UI, no orchestration layer. Those would make the
result harder to interpret, and none of them are needed to test the hypothesis.

---

## 1. The problem this reproduces

A request passes through a middleware service, which logs `request_id`. It ends up
in a payments database, which stores a row keyed by `internal_tx_hash`. An engineer
asks: *"what happened to request `f59fb16e…`?"*

There is no way to answer. The two systems share no field. And the reason is in
[`sample_app/payment_service.py`](sample_app/payment_service.py):

```python
def _tx_hash(self, req: PaymentRequest) -> str:
    """Primary key for the transactions table."""
    digest = hashlib.sha256(f"{req.request_id}:{req.user_id}".encode()).hexdigest()
    return digest[:TX_HASH_LEN]
```

The identity was not renamed. It was **destroyed and replaced** by a one-way function.
No correlation engine, no fuzzy matching, no embedding similarity and no causal ML can
invert SHA-256. The relationship exists in exactly one place: that source file.

That is the gap this project is about. Note that this is **not** a case OpenTelemetry solves:
OTel correlates *requests*, but the moment identity enters a database row, a queue message or a
batch output, trace context stops following it unless someone deliberately stored it — and schema
churn plus PII review mean they frequently didn't. (This sample app has exactly that comment in it.)

### The distractors

The file deliberately contains **three** hash-like helpers, because real code does:

| Method | What it is | Trap |
|---|---|---|
| `_idempotency_key` | `md5(order_id + user_id)` | Right shape, wrong purpose |
| `_audit_token` | `b64encode(order_id)` | Also not a join key |
| `_tx_hash` | `sha256(request_id + ":" + user_id)[:16]` | ✅ the real one |

A model that pattern-matches on "find the hashing function" picks the wrong one. It has
to actually read what each is *used for*.

---

## 2. The four stages

```
   ┌──────────────┐   expression string    ┌──────────────┐
   │  1. PROPOSE  │ ─────────────────────► │  2. COMPILE  │
   │  LLM, creative                        │  strict, no LLM
   │  reads the code                       │  restricted AST
   └──────────────┘                        └──────┬───────┘
          ▲                                       │ callable
          │ failure becomes evidence              ▼
          │                                ┌──────────────┐
          └────────────────────────────────│  3. VERIFY   │
                                           │  data, no LLM │
                                           │  join replay  │
                                           └──────┬───────┘
                                                  │ verified
                                                  ▼
                                           ┌──────────────┐
                                           │  4. PERSIST  │
                                           │ correlation_map.json
                                           └──────────────┘
```

**The rule that governs all of it:** the model is allowed to be creative about *what*
the mapping might be, because reading messy code is a language problem. It is never
allowed to be authoritative about *whether* it is correct, because that is a data
problem.

### Stage 1 — PROPOSE ([`spike/proposer.py`](spike/proposer.py))

The only place an LLM is involved. It receives the source code, the field names and
sample rows the source system logs, and sample values of the target field. It returns a
structured proposal: an expression, the fields it reads, and a verbatim quote of the
line it relied on.

Two implementations:

- **`AnthropicProposer`** — real Claude. Uses `client.messages.parse(...)` with a
  Pydantic schema, so the response is *guaranteed* to be shaped correctly. There is no
  "parse the model's prose and hope" step anywhere in this code.
- **`StubProposer`** — scripted, needs no API key. It is deliberately written to get it
  wrong first, so the retry path is exercised (and tested) offline.

> **Why evidence quotes matter.** Every proposal must cite the line it came from. A
> human can audit the claim in ten seconds, and a citation is much harder to hallucinate
> coherently than a conclusion.

### Stage 2 — COMPILE ([`spike/dsl.py`](spike/dsl.py))

The model returns a *string*. Running a string an LLM wrote is how you get owned.

So this stage parses the expression with Python's `ast` module and walks it against an
explicit whitelist. **`eval()` and `exec()` appear nowhere in this project.** The tree is
interpreted by our own ~60-line evaluator, so the only operations that can ever execute
are the ones enumerated in `FUNCTIONS`.

```
allowed   sha256 sha1 md5 lower upper strip reverse replace split
          regex_extract b64encode b64decode concat
          +  (concatenation)    [a:b]  (slicing, integer literals only)
rejected  everything else
```

Try it:

```
$ python -c "
import sys; sys.path.insert(0,'.')
from spike.dsl import compile_transform
compile_transform('__import__(\"os\").system(\"rm -rf /\")')"

CompileError: method calls and attribute access are not allowed
```

**A rejection here is a success, not a bug.** A bad idea that never reaches data cost
nothing. This is the layer that makes it safe to let a probabilistic system write code.

### Stage 3 — VERIFY ([`spike/verifier.py`](spike/verifier.py))

Take 200 real source rows. Apply the transform. Check whether the results actually exist
on the target side.

```
join_yield = matched / sampled

  ≥ 0.95   verified   trust it and cache it
  ≥ 0.50   probable   likely right with an edge case, or right with data loss
  < 0.50   rejected   the values don't exist over there at all
```

There is also a **degeneracy check**, and it is the subtlest thing in the codebase. A
transform that collapses many distinct inputs onto one output can score a *perfect*
join_yield by accident:

```python
replace(user_id, user_id, "x")   # every row → "x"; if "x" exists, 100% "match"
```

Real identity mappings are injective, so if distinct outputs drop below 90% of distinct
inputs, the edge is rejected **even at join_yield 1.0**. A transform that looks like it
worked is more dangerous than one that obviously failed. (`test_degenerate_transform_is_caught_even_when_it_joins`.)

### Stage 4 — PERSIST ([`spike/loop.py`](spike/loop.py))

Writes `out/correlation_map.json`: the expression, the evidence with file and line, and
the measured verification result. This is the artifact that later milestones cache and
re-verify when code changes.

```json
{
  "id": "edge_m0_001",
  "kind": "transform",
  "from": {"hop": "middleware-auth (Splunk)", "fields": ["request_id", "user_id"]},
  "to":   {"hop": "payments DB (Azure SQL)", "field": "internal_tx_hash"},
  "transform": {"expr": "sha256(request_id + \":\" + user_id)[:16]"},
  "evidence": [{"file": "sample_app/payment_service.py", "lines": "56-58",
                "quote": "digest = hashlib.sha256(f\"{req.request_id}:{req.user_id}\"..."}],
  "confidence": "verified",
  "verification": {"method": "join_replay", "sampled": 200, "matched": 195,
                   "join_yield": 0.975}
}
```

### The retry loop — the part that actually matters

When verification fails, the run does **not** stop. The failure is converted into
evidence and handed back to the proposer:

```
- expression: `sha256(request_id + user_id)[:16]`
  outcome: rejected
  join_yield: 0.00 (0/200 matched)
  {'request_id': 'f59fb16e…', 'user_id': '271'} produced '3f57db01fb1bfd57'
  which is not present on the target side
```

This is the difference between a prompt and a system. A single-shot proposal is a guess.
A proposal that gets measured, fails, and is re-made *with the measurement attached* is
a feedback loop — and it converges.

---

## 3. Running it

```bash
cd m0
python data/generate.py          # writes the two log files (deterministic, seeded)
python run.py --proposer stub    # offline, no API key, no dependencies
```

With real Claude:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python run.py --proposer anthropic      # defaults to claude-opus-5
```

`--proposer auto` (the default) uses Claude when `ANTHROPIC_API_KEY` is set and the stub
otherwise, so the demo never fails for want of a key.

### Getting a measurement, not an anecdote

The model is non-deterministic, so a single successful run tells you almost nothing.
`--runs N` repeats the whole spike and summarises:

```bash
.venv/bin/python run.py --proposer anthropic --runs 5 --quiet
```
```
  resolved        5/5
  mean attempts   1.4   first-try 3/5
  mean join_yield 0.975
  expressions     1 distinct: sha256(request_id + ":" + user_id)[:16]
  tokens 11204 in / 4380 out   est. $0.1655 total, 0.0331 per run
```

Every run reports tokens and estimated cost, because a run that can't say what it cost
can't be compared against anything. `out/run_log.json` keeps the full per-attempt
record.

> **Note on the stub:** `--proposer stub` is scripted to miss once and then correct.
> It verifies the *loop* — that a failed join becomes evidence and the retry converges.
> It says nothing about whether a model can actually read the code, because the answer
> is hardcoded. Only `--proposer anthropic` tests the hypothesis.

Tests:

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests -q        # 36 passed
```

Exit code is `0` when the edge resolves, `1` when it does not — so this can go straight
into CI later: re-verify the derived mappings on every merge, and fail the build when one breaks.

---

## 4. Reading the files

| File | Lines | What it is |
|---|---|---|
| [`sample_app/payment_service.py`](sample_app/payment_service.py) | ~70 | The code under analysis. Contains the real transform and two distractors |
| [`data/generate.py`](data/generate.py) | ~90 | Builds the dataset. **Imports the sample app**, so data and code can never drift |
| [`spike/dsl.py`](spike/dsl.py) | ~230 | Restricted-AST compiler + interpreter. The safety boundary |
| [`spike/verifier.py`](spike/verifier.py) | ~120 | Join replay, thresholds, degeneracy check |
| [`spike/proposer.py`](spike/proposer.py) | ~180 | Prompt construction; Claude and stub implementations |
| [`spike/loop.py`](spike/loop.py) | ~110 | The four stages, the retry, the persisted artifact |
| [`run.py`](run.py) | ~130 | CLI and the terminal rendering |
| [`tests/`](tests/) | ~180 | 36 tests, all offline |

### A deliberate detail about the dataset

`data/generate.py` drops 2% of transactions before the database write, simulating
payments that failed upstream. So **the maximum achievable `join_yield` is ~0.984, not
1.0** (`data/ground_truth.json` records the exact ceiling).

This is not decoration. A dataset where the right answer scores exactly 1.0 teaches you
to expect perfection and to treat 0.97 as failure. Real data has holes, and the
`verified` threshold of 0.95 exists to tolerate them.

---

## 5. Things to try

The fastest way to understand a system is to attack it.

1. **Break the code, watch the map break.** Change `TX_HASH_LEN` to `12` in the sample
   app, regenerate, re-run. The cached expression now fails verification. That is the
   code-drift regression signal — in a real deployment this runs in CI and fails the build
   when a derived mapping silently breaks.
2. **Make it unresolvable.** Edit `_tx_hash` to include `time.time()`. No expression in
   the language can reproduce a timestamp, so the run should end `UNRESOLVED` — and the
   correlation map should contain an empty `edges` list rather than a guess. *Reporting an
   unresolvable boundary is correct behaviour*, and is tested.
3. **Try to escape the sandbox.** Add your best attempt to the reject list in
   `tests/test_dsl.py`. If you find one that gets through, that's a real finding.
4. **Compare proposers.** Run the stub and then real Claude. Does the model find it in
   one attempt or two? Does it pick the wrong helper first?

---

## 6. What this does *not* do

Stated plainly, because the gap between a spike and a system is where side projects usually
overclaim:

- **One boundary, known in advance.** Nothing discovers *which* boundaries exist in an estate.
- **One kind of edge.** Only a derived `transform`. Two other kinds matter in practice and aren't
  here: `identity` (the value passes through unchanged, or under a standard like W3C `traceparent`
  — cheap to recognise, no reasoning needed) and `bridge` (the two IDs have *no* functional
  relationship because a gateway minted a fresh one; the only link is a pivot log record carrying
  both).
- **No agent.** A single LLM call in a `for` loop. No tool use, no planning, no ReAct loop.
- **No incident triage.** Nothing queries logs to answer a question yet. This only builds the key
  that would make that possible.
- **Self-authored data.** The environment was written to contain the answer. That is evidence the
  approach works, not proof it generalises. Running this against a real third-party repository is
  the honest next step.

---

## 7. Where this sits

This is the de-risking spike for a larger idea: a correlation map built once per codebase version,
cached, and re-verified when the code or the topology changes — so that an investigation can follow
a request across silos that were never designed to be joined.

The spike deliberately came first. The expensive, interesting work is the derivation, and if that
doesn't hold there is no point building an estate around it. The Python package is still called
`spike/` for the same reason: it is honest about what this is.

