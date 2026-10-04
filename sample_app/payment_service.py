"""Payment service - persistence layer.

Trimmed from the NorthWind payments monolith. Nothing here is interesting
except how a transaction row gets its key, which is the one thing nobody
wrote down anywhere.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass

log = logging.getLogger("payment.repository")

# The 2014 billing migration made this column CHAR(16), and changing it during
# the audit freeze was not an option, so the digest is truncated on write.
TX_HASH_LEN = 16


@dataclass
class PaymentRequest:
    request_id: str        # W3C trace-id, propagated by the auth middleware
    user_id: str
    order_id: str
    amount_cents: int


class PaymentRepository:
    """Writes to the `transactions` table."""

    def __init__(self, conn):
        self._conn = conn

    def _idempotency_key(self, req: PaymentRequest) -> str:
        """Dedup key so a retried POST doesn't double-charge.

        NOT the row key - see _tx_hash.
        """
        return hashlib.md5(f"{req.order_id}{req.user_id}".encode()).hexdigest()

    def _audit_token(self, req: PaymentRequest) -> str:
        """Opaque handle written to the audit trail. Not used for joins."""
        return base64.b64encode(req.order_id.encode()).decode()

    def _tx_hash(self, req: PaymentRequest) -> str:
        """Primary key for the transactions table."""
        digest = hashlib.sha256(f"{req.request_id}:{req.user_id}".encode()).hexdigest()
        return digest[:TX_HASH_LEN]

    def record(self, req: PaymentRequest, status: str) -> str:
        tx_hash = self._tx_hash(req)
        self._conn.execute(
            "INSERT INTO transactions (internal_tx_hash, status, amount_cents) "
            "VALUES (?, ?, ?)",
            (tx_hash, status, req.amount_cents),
        )
        # NB: we deliberately do not log request_id here - the security review
        # in 2019 flagged it as PII-adjacent because of the legacy session
        # ids that used to be embedded in it.
        log.info(
            "transaction recorded",
            extra={"internal_tx_hash": tx_hash, "status": status},
        )
        return tx_hash
