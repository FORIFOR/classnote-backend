"""
integrations_lifeselect.py — PR3 Lifeselect order/cancel service + store.

One module to mirror the project's existing "service + store in one file"
convention (see `entity_review_services.py` / `entity_review_store.py`).

Responsibilities:
    LifeselectService
        - build_request_key
        - generate_cp_identifier
        - generate_unique_licence_key (collision-safe retry)
        - order  (idempotent by identifier+link_mng_id)
        - cancel (looks up by cp_identifier+licence_key)

    LifeselectStore
        - get_license_by_request_key
        - get_active_license
        - create_license_with_retry  (wraps collision retry + create)
        - update_license
        - create_request_log  (Authorization header is never written here)
        - licence_key_exists  (helper for collision probe)

Firestore collections:
    partner_licenses       — issued credentials (lifetime record)
    integration_requests   — audit log (Authorization header excluded)
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from app.firebase import db
from app.util_models import (
    LifeselectCancelRequest,
    LifeselectCancelResponse,
    LifeselectErrorObject,
    LifeselectIssueObject,
    LifeselectOrderRequest,
    LifeselectOrderResponse,
)

logger = logging.getLogger("app.integrations_lifeselect")


PARTNER_CODE = "lifeselect"
_LICENCE_KEY_LENGTH = 10
_LICENCE_KEY_MAX_RETRIES = 5
_LICENCES_COL = "partner_licenses"
_REQUESTS_COL = "integration_requests"


# ===========================================================================
# Error codes (spec §10)
# ===========================================================================

ERR_REQUIRED = ("E001", "required field missing")
ERR_FORMAT = ("E002", "invalid format")
ERR_IDENTIFIER = ("E003", "invalid identifier")
ERR_LICENSE_NOT_FOUND = ("E101", "license not found")
ERR_ALREADY_CANCELLED = ("E102", "license already cancelled")
ERR_MISMATCH = ("E103", "cp_identifier / licence_key mismatch")
ERR_INTERNAL = ("E500", "internal server error")


def make_error(code_pair: Tuple[str, str], detail: str = "") -> LifeselectErrorObject:
    return LifeselectErrorObject(code=code_pair[0], msg=code_pair[1], detail=detail)


# ===========================================================================
# Store
# ===========================================================================

class LifeselectStore:
    """Firestore access for partner_licenses + integration_requests."""

    # --- licenses -------------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def get_license_by_request_key(self, request_key: str) -> Optional[Any]:
        """Idempotency lookup: has this partner_code + request_key been issued?"""
        docs = (
            db.collection(_LICENCES_COL)
            .where("partner_code", "==", PARTNER_CODE)
            .where("request_key", "==", request_key)
            .limit(1)
            .stream()
        )
        return next(iter(docs), None)

    def get_active_license(
        self, cp_identifier: str, licence_key: str
    ) -> Optional[Any]:
        """Cancel lookup: find the active license by cp_identifier+licence_key."""
        docs = (
            db.collection(_LICENCES_COL)
            .where("partner_code", "==", PARTNER_CODE)
            .where("cp_identifier", "==", cp_identifier)
            .where("licence_key", "==", licence_key)
            .where("status", "==", "active")
            .limit(1)
            .stream()
        )
        return next(iter(docs), None)

    def licence_key_exists(self, licence_key: str) -> bool:
        """Collision probe — any status, not just active."""
        docs = (
            db.collection(_LICENCES_COL)
            .where("partner_code", "==", PARTNER_CODE)
            .where("licence_key", "==", licence_key)
            .limit(1)
            .stream()
        )
        return next(iter(docs), None) is not None

    def create_license(self, data: Dict[str, Any]) -> None:
        db.collection(_LICENCES_COL).document().set(data)

    def update_license(self, doc_id: str, data: Dict[str, Any]) -> None:
        db.collection(_LICENCES_COL).document(doc_id).update(data)

    # --- audit log ------------------------------------------------------

    def create_request_log(self, data: Dict[str, Any]) -> None:
        """Write one integration_requests audit entry.

        Callers MUST NOT include Authorization header contents in `data`.
        """
        db.collection(_REQUESTS_COL).document().set(data)


# ===========================================================================
# Service
# ===========================================================================

class LifeselectService:
    """Business logic for Lifeselect order/cancel."""

    def __init__(self, store: Optional[LifeselectStore] = None) -> None:
        self.store = store or LifeselectStore()

    # --- key generators -------------------------------------------------

    @staticmethod
    def build_request_key(identifier: str, link_mng_id: str) -> str:
        return f"{identifier}:{link_mng_id}"

    @staticmethod
    def generate_cp_identifier(contract_date: str, link_mng_id: str) -> str:
        """DN-YYYYMMDD-<link_mng_id> (readable, partner-scoped)."""
        return f"DN-{contract_date}-{link_mng_id}"

    def _random_licence_key(self) -> str:
        return "".join(secrets.choice("0123456789") for _ in range(_LICENCE_KEY_LENGTH))

    def generate_unique_licence_key(self, max_retries: int = _LICENCE_KEY_MAX_RETRIES) -> str:
        """Produce a licence_key not currently in partner_licenses.

        PR3 v0.1: optimistic probe + regenerate on hit. Since the lookup
        Firestore query has a small TOCTOU window we accept a tiny residual
        collision risk; when it happens the next write with the same key
        still creates a new doc but readers detect duplicates via
        request_key (idempotency). Acceptable for PR3 volumes.
        """
        candidate = self._random_licence_key()
        for _ in range(max_retries):
            candidate = self._random_licence_key()
            try:
                if not self.store.licence_key_exists(candidate):
                    return candidate
            except Exception as exc:
                # Firestore unavailable? Fail open (let caller retry at request level).
                logger.warning("[lifeselect] collision probe failed: %s", exc)
                return candidate
        # Fall through on max retries — last random still returned.
        logger.error("[lifeselect] licence_key collision retries exhausted; using last")
        return candidate

    # --- order ----------------------------------------------------------

    def order(self, payload: LifeselectOrderRequest) -> LifeselectOrderResponse:
        """Idempotent order API.

        If (identifier, link_mng_id) was already issued, return the exact
        same cp_identifier / licence_key from Firestore (spec §7).
        """
        request_key = self.build_request_key(payload.identifier, payload.link_mng_id)
        existing = self.store.get_license_by_request_key(request_key)
        if existing is not None:
            data = existing.to_dict() or {}
            return LifeselectOrderResponse(
                rtn=True,
                issue=LifeselectIssueObject(
                    cp_identifier=data.get("cp_identifier", ""),
                    licence_key=data.get("licence_key", ""),
                ),
            )

        cp_identifier = self.generate_cp_identifier(
            payload.contract_date, payload.link_mng_id,
        )
        licence_key = self.generate_unique_licence_key()
        now = self.store._now()
        self.store.create_license({
            "partner_code": PARTNER_CODE,
            "request_key": request_key,
            "identifier": payload.identifier,
            "link_mng_id": payload.link_mng_id,
            "cp_identifier": cp_identifier,
            "licence_key": licence_key,
            "contract_date": payload.contract_date,
            "cancel_date": "",
            "status": "active",
            "created_at": now,
            "updated_at": now,
        })
        return LifeselectOrderResponse(
            rtn=True,
            issue=LifeselectIssueObject(
                cp_identifier=cp_identifier,
                licence_key=licence_key,
            ),
        )

    # --- cancel ---------------------------------------------------------

    def cancel(self, payload: LifeselectCancelRequest) -> LifeselectCancelResponse:
        """Cancel by cp_identifier+licence_key.

        Distinguishes between "no such license" (E101) and "already
        cancelled" (E102) to help the partner diagnose reconciliation
        issues.
        """
        # Primary lookup: active license.
        existing = self.store.get_active_license(
            payload.cp_identifier, payload.licence_key,
        )
        if existing is not None:
            # Optional extra defense: verify the identifier+link_mng_id
            # match what was on the record. Mismatches → E103.
            data = existing.to_dict() or {}
            if (
                data.get("identifier") != payload.identifier
                or data.get("link_mng_id") != payload.link_mng_id
            ):
                return LifeselectCancelResponse(
                    rtn=False,
                    error=make_error(
                        ERR_MISMATCH,
                        detail="identifier/link_mng_id do not match license",
                    ),
                )
            self.store.update_license(existing.id, {
                "cancel_date": payload.cancel_date,
                "status": "cancelled",
                "updated_at": self.store._now(),
            })
            return LifeselectCancelResponse(rtn=True)

        # Secondary lookup: maybe already cancelled?
        docs = (
            db.collection(_LICENCES_COL)
            .where("partner_code", "==", PARTNER_CODE)
            .where("cp_identifier", "==", payload.cp_identifier)
            .where("licence_key", "==", payload.licence_key)
            .limit(1)
            .stream()
        )
        found = next(iter(docs), None)
        if found is not None:
            data = found.to_dict() or {}
            if data.get("status") == "cancelled":
                return LifeselectCancelResponse(
                    rtn=False,
                    error=make_error(
                        ERR_ALREADY_CANCELLED,
                        detail=f"cancelled at {data.get('cancel_date')}",
                    ),
                )

        return LifeselectCancelResponse(
            rtn=False,
            error=make_error(
                ERR_LICENSE_NOT_FOUND,
                detail="no license matches cp_identifier/licence_key",
            ),
        )
