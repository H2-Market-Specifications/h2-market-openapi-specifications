from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from uuid import UUID

# H2-Transaction-Id, H2-Initial-Transaction-Id and H2-Reference-Id are lowercase UUIDs of version 7.
UUID7_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def new_transaction_id() -> str:
    """Return a new UUID version 7 (RFC 9562): 48-bit Unix time in milliseconds followed by random bits."""
    timestamp_ms = time.time_ns() // 1_000_000
    random_bits = int.from_bytes(os.urandom(10), "big")
    rand_a = random_bits >> 68
    rand_b = random_bits & ((1 << 62) - 1)
    value = (timestamp_ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return str(UUID(int=value))


def is_transaction_id(value: str | None) -> bool:
    return value is not None and UUID7_PATTERN.fullmatch(value) is not None


def effective_transaction_id(headers: Mapping[str, str]) -> str | None:
    """Return the effective transaction ID of a request, or None if it has no syntactically valid H2-Transaction-Id.

    For a retry this is the H2-Initial-Transaction-Id; if that value is invalid, the current H2-Transaction-Id is used
    because the effective transaction ID cannot be determined reliably.
    """
    transaction_id = headers.get("h2-transaction-id")
    if not is_transaction_id(transaction_id):
        return None
    initial_transaction_id = headers.get("h2-initial-transaction-id")
    return initial_transaction_id if is_transaction_id(initial_transaction_id) else transaction_id
