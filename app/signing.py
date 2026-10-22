"""Webhook signatures: X-Signature = hex HMAC-SHA256 of the raw request body with a shared secret.

Shared by the service (verifies) and the fake providers (sign), so both sides use one implementation.
"""

import hashlib
import hmac

SIGNATURE_HEADER = "X-Signature"


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    expected = sign(secret, body).encode()
    # compare_digest on bytes is constant-time and copes with any (even non-ASCII) input.
    return hmac.compare_digest(expected, signature.strip().lower().encode())
