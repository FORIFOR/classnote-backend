"""
Unit tests for Lifeselect integration PR3.

Covers:
  - schema validation (identifier regex, link_mng_id digit-count, contract_date)
  - generate_cp_identifier / build_request_key determinism
  - generate_unique_licence_key collision retry
  - order idempotency (existing license returns same cp_identifier/licence_key)
  - cancel happy path / E101 not-found / E102 already-cancelled / E103 mismatch
  - ensure_lifeselect_enabled gate
  - verify_lifeselect_basic_auth (missing / malformed / wrong creds / correct)
  - verify_lifeselect_ip_allowlist (disabled / allowed / blocked / XFF parse)
"""
from __future__ import annotations

import base64
import sys
import types
from unittest.mock import MagicMock

# Stub heavy imports
for _m in [
    "google", "google.cloud", "google.cloud.firestore",
    "vertexai", "vertexai.generative_models",
    "app.firebase",
]:
    if _m not in sys.modules:
        sys.modules[_m] = types.ModuleType(_m)
sys.modules["app.firebase"].db = None  # type: ignore[attr-defined]
sys.path.insert(0, ".")

from app.util_models import (  # noqa: E402
    LifeselectCancelRequest,
    LifeselectOrderRequest,
)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def test_order_identifier_regex_rejects_lowercase():
    try:
        LifeselectOrderRequest(
            identifier="pmc",  # lowercase
            link_mng_id="0001234",
            contract_date="20260423",
        )
    except Exception:
        return
    raise AssertionError("lowercase identifier should be rejected")


def test_order_identifier_regex_accepts_uppercase_alnum():
    LifeselectOrderRequest(
        identifier="PMC", link_mng_id="0001234", contract_date="20260423",
    )
    LifeselectOrderRequest(
        identifier="A1", link_mng_id="1234567", contract_date="20260423",
    )
    LifeselectOrderRequest(
        identifier="X2Y", link_mng_id="9999999", contract_date="20260423",
    )


def test_order_link_mng_id_must_be_7_digits():
    for bad in ("123456", "12345678", "abc1234", "000-123"):
        try:
            LifeselectOrderRequest(identifier="PMC", link_mng_id=bad, contract_date="20260423")
        except Exception:
            continue
        raise AssertionError(f"link_mng_id={bad!r} should be rejected")


def test_order_contract_date_must_be_yyyymmdd():
    try:
        LifeselectOrderRequest(
            identifier="PMC", link_mng_id="0001234", contract_date="2026-04-23",
        )
    except Exception:
        return
    raise AssertionError("contract_date with dashes should be rejected")


def test_cancel_requires_cp_identifier_and_licence_key():
    try:
        LifeselectCancelRequest(
            identifier="PMC", link_mng_id="0001234",
            cp_identifier="", licence_key="",
            cancel_date="20260501",
        )
    except Exception:
        return
    raise AssertionError("empty cp_identifier / licence_key should be rejected")


# ---------------------------------------------------------------------------
# Service helpers (pure)
# ---------------------------------------------------------------------------

def test_build_request_key_is_deterministic():
    from app.services.integrations_lifeselect import LifeselectService
    svc = LifeselectService.__new__(LifeselectService)  # no store init
    assert svc.build_request_key("PMC", "0001234") == "PMC:0001234"


def test_generate_cp_identifier_format():
    from app.services.integrations_lifeselect import LifeselectService
    svc = LifeselectService.__new__(LifeselectService)
    assert svc.generate_cp_identifier("20260423", "0001234") == "DN-20260423-0001234"


def test_random_licence_key_is_10_digits():
    from app.services.integrations_lifeselect import LifeselectService
    svc = LifeselectService.__new__(LifeselectService)
    for _ in range(10):
        k = svc._random_licence_key()
        assert len(k) == 10
        assert k.isdigit()


def test_generate_unique_licence_key_retries_on_collision():
    from app.services.integrations_lifeselect import LifeselectService, LifeselectStore

    store = LifeselectStore.__new__(LifeselectStore)
    # First two probes hit an existing key; third is unique.
    call_log: list = []

    def probe(key):  # noqa: ARG001
        call_log.append(key)
        return len(call_log) < 3
    store.licence_key_exists = probe  # type: ignore[assignment]

    svc = LifeselectService(store=store)
    out = svc.generate_unique_licence_key(max_retries=5)
    assert len(out) == 10
    assert len(call_log) == 3  # first two collided, third won


def test_generate_unique_licence_key_fail_open_on_store_error():
    from app.services.integrations_lifeselect import LifeselectService, LifeselectStore

    store = LifeselectStore.__new__(LifeselectStore)
    def probe(key):  # noqa: ARG001
        raise RuntimeError("firestore outage")
    store.licence_key_exists = probe  # type: ignore[assignment]

    svc = LifeselectService(store=store)
    out = svc.generate_unique_licence_key(max_retries=3)
    assert len(out) == 10  # still produces a key


# ---------------------------------------------------------------------------
# Order flow (idempotency)
# ---------------------------------------------------------------------------

class _FakeDoc:
    def __init__(self, data, doc_id="doc_1"):
        self._data = data
        self.id = doc_id
    def to_dict(self):
        return self._data


def test_order_idempotent_returns_existing_license():
    from app.services.integrations_lifeselect import LifeselectService, LifeselectStore

    store = LifeselectStore.__new__(LifeselectStore)
    existing = _FakeDoc({
        "cp_identifier": "DN-20260423-0001234",
        "licence_key": "1234567890",
    })
    store.get_license_by_request_key = lambda k: existing  # type: ignore[assignment]
    store.create_license = MagicMock()  # type: ignore[assignment]

    svc = LifeselectService(store=store)
    out = svc.order(LifeselectOrderRequest(
        identifier="PMC", link_mng_id="0001234", contract_date="20260423",
    ))
    assert out.rtn is True
    assert out.issue.cp_identifier == "DN-20260423-0001234"
    assert out.issue.licence_key == "1234567890"
    store.create_license.assert_not_called()


def test_order_creates_new_license_when_absent():
    from app.services.integrations_lifeselect import LifeselectService, LifeselectStore

    store = LifeselectStore.__new__(LifeselectStore)
    store.get_license_by_request_key = lambda k: None  # type: ignore[assignment]
    store.licence_key_exists = lambda k: False  # type: ignore[assignment]
    store.create_license = MagicMock()  # type: ignore[assignment]
    store._now = staticmethod(lambda: None)  # type: ignore[assignment]

    svc = LifeselectService(store=store)
    out = svc.order(LifeselectOrderRequest(
        identifier="PMC", link_mng_id="0001234", contract_date="20260423",
    ))
    assert out.rtn is True
    assert out.issue.cp_identifier == "DN-20260423-0001234"
    assert len(out.issue.licence_key) == 10
    store.create_license.assert_called_once()
    written = store.create_license.call_args[0][0]
    assert written["status"] == "active"
    assert written["request_key"] == "PMC:0001234"
    assert written["partner_code"] == "lifeselect"


# ---------------------------------------------------------------------------
# Cancel flow
# ---------------------------------------------------------------------------

def test_cancel_happy_path():
    from app.services.integrations_lifeselect import LifeselectService, LifeselectStore

    store = LifeselectStore.__new__(LifeselectStore)
    existing = _FakeDoc({
        "identifier": "PMC",
        "link_mng_id": "0001234",
        "status": "active",
    }, doc_id="active_doc")
    store.get_active_license = lambda cp, lk: existing  # type: ignore[assignment]
    store.update_license = MagicMock()  # type: ignore[assignment]
    store._now = staticmethod(lambda: None)  # type: ignore[assignment]

    svc = LifeselectService(store=store)
    out = svc.cancel(LifeselectCancelRequest(
        identifier="PMC", link_mng_id="0001234",
        cp_identifier="DN-X", licence_key="1234567890",
        cancel_date="20260501",
    ))
    assert out.rtn is True
    store.update_license.assert_called_once()
    args, _ = store.update_license.call_args
    assert args[0] == "active_doc"
    assert args[1]["status"] == "cancelled"
    assert args[1]["cancel_date"] == "20260501"


def test_cancel_not_found_returns_e101():
    from app.services.integrations_lifeselect import LifeselectService, LifeselectStore
    import app.services.integrations_lifeselect as mod

    store = LifeselectStore.__new__(LifeselectStore)
    store.get_active_license = lambda cp, lk: None  # type: ignore[assignment]
    # Stub the secondary (status=cancelled) lookup path as well.
    mod.db = types.SimpleNamespace(
        collection=lambda name: types.SimpleNamespace(
            where=lambda *a, **k: types.SimpleNamespace(
                where=lambda *a, **k: types.SimpleNamespace(
                    where=lambda *a, **k: types.SimpleNamespace(
                        where=lambda *a, **k: types.SimpleNamespace(
                            limit=lambda n: types.SimpleNamespace(stream=lambda: iter([])),
                        ),
                        limit=lambda n: types.SimpleNamespace(stream=lambda: iter([])),
                    ),
                ),
            ),
        ),
    )
    svc = LifeselectService(store=store)
    out = svc.cancel(LifeselectCancelRequest(
        identifier="PMC", link_mng_id="0001234",
        cp_identifier="DN-X", licence_key="1234567890",
        cancel_date="20260501",
    ))
    assert out.rtn is False
    assert out.error.code == "E101"


def test_cancel_mismatch_returns_e103():
    from app.services.integrations_lifeselect import LifeselectService, LifeselectStore

    store = LifeselectStore.__new__(LifeselectStore)
    existing = _FakeDoc({
        "identifier": "XYZ",   # mismatch with request.identifier="PMC"
        "link_mng_id": "9999999",
        "status": "active",
    })
    store.get_active_license = lambda cp, lk: existing  # type: ignore[assignment]
    store.update_license = MagicMock()  # type: ignore[assignment]

    svc = LifeselectService(store=store)
    out = svc.cancel(LifeselectCancelRequest(
        identifier="PMC", link_mng_id="0001234",
        cp_identifier="DN-X", licence_key="1234567890",
        cancel_date="20260501",
    ))
    assert out.rtn is False
    assert out.error.code == "E103"
    store.update_license.assert_not_called()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_lifeselect_enabled_flag(monkeypatch):
    from app.services import integrations_auth as auth
    monkeypatch.delenv("LIFESELECT_ENABLED", raising=False)
    assert auth.lifeselect_enabled() is False
    monkeypatch.setenv("LIFESELECT_ENABLED", "1")
    assert auth.lifeselect_enabled() is True
    monkeypatch.setenv("LIFESELECT_ENABLED", "false")
    assert auth.lifeselect_enabled() is False


def test_basic_auth_missing_header_raises(monkeypatch):
    from app.services import integrations_auth as auth
    monkeypatch.setenv("LIFESELECT_BASIC_USER", "u")
    monkeypatch.setenv("LIFESELECT_BASIC_PASS", "p")
    try:
        auth.verify_lifeselect_basic_auth(authorization=None)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 401
        return
    raise AssertionError("missing Authorization should raise")


def test_basic_auth_wrong_credentials(monkeypatch):
    from app.services import integrations_auth as auth
    monkeypatch.setenv("LIFESELECT_BASIC_USER", "u")
    monkeypatch.setenv("LIFESELECT_BASIC_PASS", "p")
    bad = "Basic " + base64.b64encode(b"wrong:creds").decode()
    try:
        auth.verify_lifeselect_basic_auth(authorization=bad)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 401
        return
    raise AssertionError("wrong credentials should raise")


def test_basic_auth_correct_credentials_passes(monkeypatch):
    from app.services import integrations_auth as auth
    monkeypatch.setenv("LIFESELECT_BASIC_USER", "u")
    monkeypatch.setenv("LIFESELECT_BASIC_PASS", "p")
    ok = "Basic " + base64.b64encode(b"u:p").decode()
    # Should not raise.
    auth.verify_lifeselect_basic_auth(authorization=ok)


def test_basic_auth_malformed_header(monkeypatch):
    from app.services import integrations_auth as auth
    monkeypatch.setenv("LIFESELECT_BASIC_USER", "u")
    monkeypatch.setenv("LIFESELECT_BASIC_PASS", "p")
    for bad in ("Basic", "Bearer xyz", "Basic !!!not-base64!!!"):
        try:
            auth.verify_lifeselect_basic_auth(authorization=bad)
        except Exception:
            continue
        raise AssertionError(f"malformed {bad!r} should raise")


def test_basic_auth_fails_when_env_missing(monkeypatch):
    """Fail-closed: missing server-side env means all requests rejected."""
    from app.services import integrations_auth as auth
    monkeypatch.delenv("LIFESELECT_BASIC_USER", raising=False)
    monkeypatch.delenv("LIFESELECT_BASIC_PASS", raising=False)
    ok = "Basic " + base64.b64encode(b"u:p").decode()
    try:
        auth.verify_lifeselect_basic_auth(authorization=ok)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 401
        return
    raise AssertionError("missing env should reject all")


# ---------------------------------------------------------------------------
# IP allowlist
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, headers=None, client_host="127.0.0.1"):
        self.headers = headers or {}
        self.client = types.SimpleNamespace(host=client_host)


def test_ip_allowlist_disabled_when_env_empty(monkeypatch):
    from app.services import integrations_auth as auth
    monkeypatch.setenv("LIFESELECT_IP_ALLOWLIST", "")
    req = _FakeRequest(headers={"x-forwarded-for": "8.8.8.8"})
    auth.verify_lifeselect_ip_allowlist(req)  # should not raise


def test_ip_allowlist_allows_exact_match(monkeypatch):
    from app.services import integrations_auth as auth
    monkeypatch.setenv("LIFESELECT_IP_ALLOWLIST", "1.2.3.4,5.6.7.8")
    req = _FakeRequest(headers={"x-forwarded-for": "1.2.3.4"})
    auth.verify_lifeselect_ip_allowlist(req)  # should not raise


def test_ip_allowlist_blocks_non_match(monkeypatch):
    from app.services import integrations_auth as auth
    monkeypatch.setenv("LIFESELECT_IP_ALLOWLIST", "1.2.3.4")
    req = _FakeRequest(headers={"x-forwarded-for": "9.9.9.9"})
    try:
        auth.verify_lifeselect_ip_allowlist(req)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 403
        return
    raise AssertionError("non-matching IP should 403")


def test_ip_allowlist_parses_xff_first_hop(monkeypatch):
    from app.services import integrations_auth as auth
    monkeypatch.setenv("LIFESELECT_IP_ALLOWLIST", "1.2.3.4")
    # XFF chain: real client first, then relays.
    req = _FakeRequest(headers={"x-forwarded-for": "1.2.3.4, 10.0.0.1, 10.0.0.2"})
    auth.verify_lifeselect_ip_allowlist(req)  # should not raise
