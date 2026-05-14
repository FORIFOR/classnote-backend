"""Unit tests for license key generation / normalisation / hashing.

Pure-Python only — no Firestore needed.
"""

from __future__ import annotations

import re

import pytest

from app.services.license_key_service import (
    DEFAULT_KEY_PREFIX,
    LICENSE_KEY_ALPHABET,
    generate_license_key,
    generate_license_keys,
    hash_license_key,
    last4_of_key,
    normalise_license_key,
    prefix_of_key,
)

_KEY_RE = re.compile(r"^DNLS-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")


def test_generate_license_key_matches_format() -> None:
    k = generate_license_key()
    assert _KEY_RE.fullmatch(k), f"unexpected shape: {k!r}"
    # No look-alike characters in any random portion.
    for ch in k.replace("DNLS-", "").replace("-", ""):
        assert ch in LICENSE_KEY_ALPHABET, f"bad char {ch!r}"
    assert "I" not in k and "O" not in k and "0" not in k and "1" not in k


def test_generate_license_keys_unique_and_correct_count() -> None:
    keys = generate_license_keys(500)
    assert len(keys) == 500
    assert len(set(keys)) == 500
    for k in keys:
        assert _KEY_RE.fullmatch(k)


def test_generate_license_keys_rejects_bad_count() -> None:
    with pytest.raises(ValueError):
        generate_license_keys(0)
    with pytest.raises(ValueError):
        generate_license_keys(200_000)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("DNLS-7K4P-X9M2-Q8AZ", "DNLS7K4PX9M2Q8AZ"),
        ("dnls-7k4p-x9m2-q8az", "DNLS7K4PX9M2Q8AZ"),
        (" dnls 7k4p x9m2 q8az ", "DNLS7K4PX9M2Q8AZ"),
        ("DNLS7K4P_X9M2_Q8AZ", "DNLS7K4PX9M2Q8AZ"),
        ("DNLS-7K4P-X9M2-Q8AZ\n", "DNLS7K4PX9M2Q8AZ"),
    ],
)
def test_normalise_license_key(raw: str, expected: str) -> None:
    assert normalise_license_key(raw) == expected


def test_normalise_license_key_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        normalise_license_key(123)  # type: ignore[arg-type]


def test_hash_is_stable_and_case_insensitive() -> None:
    h1 = hash_license_key("DNLS-7K4P-X9M2-Q8AZ")
    h2 = hash_license_key("dnls 7k4p x9m2 q8az")
    h3 = hash_license_key("DNLS7K4PX9M2Q8AZ")
    assert h1 == h2 == h3
    assert len(h1) == 64
    # Different key → different hash.
    assert hash_license_key("DNLS-AAAA-BBBB-CCCC") != h1


def test_last4_and_prefix() -> None:
    k = "DNLS-7K4P-X9M2-Q8AZ"
    assert last4_of_key(k) == "Q8AZ"
    assert last4_of_key(" dnls-7K4P-X9M2-q8az ") == "Q8AZ"
    assert prefix_of_key(k) == "DNLS"
    assert prefix_of_key("") == DEFAULT_KEY_PREFIX
    assert prefix_of_key("FOO-BAR-BAZ-QUX") == "FOO"
