"""Generate the two log files for the M0 spike.

The ground truth comes from the real sample-app code, so the data and the
code can never drift apart. `ground_truth.json` is for the test suite only -
it is never shown to the proposer.
"""

from __future__ import annotations

import json
import pathlib
import random
import sys
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from sample_app.payment_service import PaymentRepository, PaymentRequest  # noqa: E402

N_REQUESTS = 500
DB_WRITE_FAILURE_RATE = 0.02  # ceiling on achievable join_yield is ~0.98
NOISE_ROWS = 300
SEED = 20260919


def main() -> None:
    rng = random.Random(SEED)
    repo = PaymentRepository(conn=None)
    t0 = datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc)

    middleware, appdb = [], []

    for i in range(N_REQUESTS):
        req = PaymentRequest(
            request_id="".join(rng.choices("0123456789abcdef", k=32)),
            user_id=str(rng.randint(100, 999)),
            order_id=f"ORD-{rng.randint(10**5, 10**6 - 1)}",
            amount_cents=rng.randint(500, 250_000),
        )
        ts = t0 + timedelta(milliseconds=i * 137)

        middleware.append({
            "ts": ts.isoformat(),
            "level": "INFO",
            "svc": "middleware-auth",
            "request_id": req.request_id,
            "user_id": req.user_id,
            "order_id": req.order_id,
            "route": "POST /v2/payments",
            "msg": "forwarding to payment-api",
        })

        if rng.random() >= DB_WRITE_FAILURE_RATE:
            appdb.append({
                "ts": (ts + timedelta(milliseconds=rng.randint(20, 400))).isoformat(),
                "table": "transactions",
                "internal_tx_hash": repo._tx_hash(req),
                "status": "SETTLED" if rng.random() > 0.08 else "DECLINED",
                "amount_cents": req.amount_cents,
            })

    # Unrelated traffic, so neither file is a clean 1:1 list.
    for i in range(NOISE_ROWS):
        ts = t0 + timedelta(milliseconds=i * 211)
        middleware.append({
            "ts": ts.isoformat(), "level": "DEBUG", "svc": "middleware-auth",
            "msg": "health probe ok", "route": "GET /healthz",
        })
        appdb.append({
            "ts": ts.isoformat(), "table": "audit_log",
            "event": "session.refresh", "actor": f"svc-{rng.randint(1, 9)}",
        })

    rng.shuffle(middleware)
    rng.shuffle(appdb)

    _write(HERE / "middleware.jsonl", middleware)
    _write(HERE / "appdb.jsonl", appdb)
    (HERE / "ground_truth.json").write_text(json.dumps({
        "expression": 'sha256(request_id + ":" + user_id)[:16]',
        "inputs": ["request_id", "user_id"],
        "target_field": "internal_tx_hash",
        "max_achievable_join_yield": round(
            sum(1 for r in appdb if "internal_tx_hash" in r) / N_REQUESTS, 4),
    }, indent=2) + "\n")

    print(f"middleware.jsonl  {len(middleware):>5} rows")
    print(f"appdb.jsonl       {len(appdb):>5} rows")
    print(f"joinable pairs    {sum(1 for r in appdb if 'internal_tx_hash' in r):>5}")


def _write(path: pathlib.Path, rows: list[dict]) -> None:
    with path.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main()
