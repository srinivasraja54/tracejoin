# tracejoin

**Can an LLM read source code and recover a join key that no telemetry could reveal, and can we
prove it got it right?**

## The problem

A middleware service logs `request_id`. The payments database stores rows keyed by
`internal_tx_hash`. The two share no field, because of this code in
[`sample_app/payment_service.py`](sample_app/payment_service.py):

```python
digest = hashlib.sha256(f"{req.request_id}:{req.user_id}".encode()).hexdigest()
return digest[:TX_HASH_LEN]
```

No correlation engine can invert SHA-256. The relationship exists only in the source code.
The file also contains two decoy hash helpers (`_idempotency_key`, `_audit_token`), so the
model has to work out which one actually produces the row key.

## Result

With Claude Opus 5.5, across 5 runs:

```
  resolved     5/5
  mean attempts 1.0   first-try 5/5
  mean join_yield 0.975
  expressions  1 distinct: sha256(request_id + ":" + user_id)[:16]
  tokens 14105 in / 2157 out   est. $0.0996 total, 0.0199 per run
```

Every run found the correct expression on the first try, ignored the decoys, and cited the right
lines. A join_yield of 0.975 is near the ceiling of 0.984, because the dataset deliberately drops
2% of payments. **On this sample, the idea works.**

The model only proposes. Each answer is compiled in a sandbox and checked against real data
before it is trusted. See [ARCHITECTURE.md](ARCHITECTURE.md).

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python data/generate.py                      # build the sample data (seeded)
.venv/bin/python run.py --proposer stub                # offline, scripted demo
export ANTHROPIC_API_KEY='sk-ant-...'
.venv/bin/python run.py --proposer anthropic --model claude-opus-5-5
.venv/bin/python run.py --proposer anthropic --model claude-opus-5-5 --runs 5 --quiet
.venv/bin/python -m pytest tests -q                    # 36 tests, offline
```

- **Stub vs real model:** the stub is scripted to miss once and then correct itself, which
  exercises the retry loop. It doesn't test the model; only `--proposer anthropic` does.
- **Output:** results are written to `out/correlation_map.json` and `out/run_log.json`.
- **Exit code:** `0` if the key was resolved, `1` if not.

## Limitations

- **The sample code labels itself.** Its docstrings say which helper is the row key, which
  makes the task easier than real code.
- **The retry loop is untested with a real model.** Claude never missed, so only the stub has
  exercised it.
- **The setup is narrow.** It covers one known boundary and one kind of mapping (a derived
  transform), and the data was written for this test.
- **Next step:** run it against a real third-party codebase.
