"""License key generation, normalisation, and hashing.

The plain key is the only credential — it must be:
- random enough that brute force is infeasible (entropy ≥ 60 bits);
- printable / hand-typeable (alphabet avoids look-alike chars ``IO01``);
- comparable case-insensitively and with optional dashes / whitespace
  (users will paste with or without dashes from email / CSV);
- hashed before storage so that a DB leak does not let an attacker
  redeem unredeemed keys.

Storage shape:
  ``keyHash``   = sha256(normalised plain key)         — primary lookup key
  ``keyPrefix`` = "DNLS" (or future per-product prefix) — for human triage
  ``keyLast4``  = last 4 chars of normalised plain key — for UI display

The plain key is NEVER persisted to Firestore. The batch-creation flow
returns it once in the API response (and CSV), then forgets it.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Iterable

# Crockford-style alphabet: no ``I``/``O``/``0``/``1`` to reduce
# transcription errors when users hand-type from a CSV.
LICENSE_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# Single canonical product prefix in Phase 1. Future tiers can introduce
# new prefixes (e.g. ``DNED`` for education licenses) without breaking
# existing keys, since the prefix is part of the hash input.
DEFAULT_KEY_PREFIX = "DNLS"

# Per-group length (3 groups × 4 chars = 12 random chars). 12 chars over a
# 32-symbol alphabet = 60 bits of entropy, well above the offline-brute
# floor for sha256.
_GROUPS = 3
_GROUP_LEN = 4

_NORMALISE_STRIP_RE = re.compile(r"[\s\-_]+")


def generate_license_key(prefix: str = DEFAULT_KEY_PREFIX) -> str:
    """Return a new random license key like ``DNLS-7K4P-X9M2-Q8AZ``.

    Uses ``secrets`` (CSPRNG). Not seeded by anything user-visible.
    """
    groups = [
        "".join(secrets.choice(LICENSE_KEY_ALPHABET) for _ in range(_GROUP_LEN))
        for _ in range(_GROUPS)
    ]
    return f"{prefix}-" + "-".join(groups)


def generate_license_keys(count: int, prefix: str = DEFAULT_KEY_PREFIX) -> list[str]:
    """Generate ``count`` unique keys.

    With 60 bits of entropy a collision in a batch of 10k is ~10⁻¹³, but
    we still de-duplicate defensively so a hypothetical future smaller
    alphabet can't cause silent overwrites at the storage layer.
    """
    if count < 1:
        raise ValueError("count must be >= 1")
    if count > 100_000:
        raise ValueError("count too large (cap=100000)")

    seen: set[str] = set()
    out: list[str] = []
    while len(out) < count:
        k = generate_license_key(prefix=prefix)
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def normalise_license_key(raw: str) -> str:
    """Canonicalise a user-entered key for hashing / comparison.

    - upper-cases
    - strips ASCII whitespace, dashes, and underscores anywhere
    - leaves remaining alphanumerics in original order

    ``DNLS-7K4P-X9M2-Q8AZ``, ``dnls 7k4p x9m2 q8az``, ``DNLS7K4PX9M2Q8AZ``
    all normalise to ``DNLS7K4PX9M2Q8AZ``.
    """
    if not isinstance(raw, str):
        raise TypeError("license key must be a string")
    return _NORMALISE_STRIP_RE.sub("", raw).upper()


def hash_license_key(raw: str) -> str:
    """SHA-256 hash of the normalised key, hex-encoded.

    Used as the Firestore document lookup. A constant-time compare is
    not required here — the hash itself is what we store and index on.
    """
    return hashlib.sha256(normalise_license_key(raw).encode("utf-8")).hexdigest()


def last4_of_key(raw: str) -> str:
    """Return the last 4 chars of the normalised key for UI display."""
    norm = normalise_license_key(raw)
    return norm[-4:] if len(norm) >= 4 else norm


def prefix_of_key(raw: str, fallback: str = DEFAULT_KEY_PREFIX) -> str:
    """Return the leading group (everything before the first dash in the
    *original* form). Used only for human triage in the admin list view.
    """
    if not isinstance(raw, str):
        return fallback
    head, _, _ = raw.strip().partition("-")
    return head.upper() if head else fallback


def keys_to_hash_map(keys: Iterable[str]) -> dict[str, str]:
    """Return ``{keyHash: plainKey}`` for a batch — useful when persisting
    a freshly generated batch where the caller still has the plain keys.
    """
    return {hash_license_key(k): k for k in keys}
