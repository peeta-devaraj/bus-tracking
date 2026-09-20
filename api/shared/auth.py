"""Per-bus identity and request signing.

Every bus registered in the system gets its own secret. A position report is
accepted only if it carries an HMAC-SHA256 signature over the *exact* request
body, computed with that bus's secret.

Signing the raw body rather than a reconstructed canonical string is a
deliberate choice: it removes any chance of the browser and the server
disagreeing about number formatting, which is the usual source of
maddening signature mismatches.

Replay is handled separately by the timestamp and nonce carried inside the
body -- see validation.check_freshness.

NOTE ON SECRET STORAGE: the server needs the secret itself to recompute the
HMAC, so it cannot store only a hash. Secrets sit in Table Storage, which is
encrypted at rest. Moving them into Azure Key Vault is the documented
hardening step and is listed in docs/threat-model.md.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

SIGNATURE_HEADER = "X-Bus-Signature"
BUS_ID_HEADER = "X-Bus-Id"


def new_secret(nbytes: int = 32) -> str:
    """Generate a fresh URL-safe secret for a newly registered bus."""
    return secrets.token_urlsafe(nbytes)


def sign(secret: str, body: bytes) -> str:
    """HMAC-SHA256 of the raw request body, as lowercase hex."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, provided_signature: str) -> bool:
    """Constant-time check of a provided signature against the expected one."""
    if not provided_signature:
        return False
    expected = sign(secret, body)
    return hmac.compare_digest(expected, provided_signature.strip().lower())


def new_nonce() -> str:
    """Short random value included in each ping to make replays detectable."""
    return secrets.token_hex(8)


def looks_like_local_emulator(storage_connection: str | None) -> bool:
    """Is this pointing at Azurite on this machine rather than a real account?"""
    conn = (storage_connection or "").strip().lower()
    if not conn:
        return False
    return (
        conn.startswith("usedevelopmentstorage=true")
        or "127.0.0.1:1000" in conn
        or "localhost:1000" in conn
        or "devstoreaccount1" in conn
    )


def admin_allowed(
    provided_key: str | None,
    expected_key: str | None,
    storage_connection: str | None,
) -> bool:
    """May this request use the admin endpoints?

    Those endpoints hand out bus secrets and rewrite routes, so this fails
    *closed*. An earlier version returned True whenever ADMIN_KEY was unset,
    to keep local development frictionless. That is the wrong default for
    anything holding secrets: a cleared app setting, or a deploy done by some
    other route than infra/deploy.ps1, would silently leave every admin
    endpoint open to the internet.

    With no key configured, access is allowed only when storage is the local
    emulator -- which is exactly the local-development case, and never Azure.
    """
    if expected_key:
        return hmac.compare_digest(provided_key or "", expected_key)
    return looks_like_local_emulator(storage_connection)
