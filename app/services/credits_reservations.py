"""
credits_reservations.py - finalize v2 の per-feature credits 予約管理。

設計メモ:
  - reservation doc は `sessions/{sid}/credits_reservations/{reservationId}`。
    reservationId は client 提供の clientRequestId を正規化したもの(= idempotency key)。
  - reserve() は冪等: 同じ clientRequestId で呼ばれた場合は既存 reservation を返す。
  - 実課金は reserve 時に ai_credits.consume() で即時引き落とし(hold 専用 counter 未実装のため)。
    commit = 成功確定(credits の移動なし)、release = 失敗時の refund。
  - feature 粒度で consume/refund するのは、ai_credits_by_mode.{mode} の
    既存 analytics を壊さないため。
  - ai_credits.consume() が途中で失敗した場合は、すでに引いた分を release する。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from google.cloud import firestore

from app.firebase import db
from app.services.ai_credits import ai_credits
from app.services.finalize_helpers import DerivedPlan, FEATURE_PRICE, plan_to_features


_SESSIONS = "sessions"
_RESERVATIONS = "credits_reservations"


# feature → ai_credits mode key(CREDIT_COST の mode と合わせる)
FEATURE_CREDIT_MODE: Dict[str, str] = {
    "summary": "summary_generated",
    "highlights": "summary_generated",
    "quiz": "quiz_generated",
}


class ReservationError(Exception):
    """reservation 作成失敗(残高不足、内部エラー等)。"""


class InsufficientCreditsError(ReservationError):
    """credits 不足で reserve できない。"""


def _res_ref(session_id: str, reservation_id: str):
    return (
        db.collection(_SESSIONS)
        .document(session_id)
        .collection(_RESERVATIONS)
        .document(reservation_id)
    )


def _get_existing(session_id: str, reservation_id: str) -> Optional[Dict[str, Any]]:
    snap = _res_ref(session_id, reservation_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    data["reservationId"] = reservation_id
    return data


def reserve(
    *,
    session_id: str,
    account_id: str,
    uid: str,
    plan: DerivedPlan,
    client_request_id: str,
    operation_id: str,
) -> Dict[str, Any]:
    """reservation を作成して credits を引き落とす。

    冪等: 同じ `client_request_id` を持つ既存 reservation があれば、
    そのまま返して課金は行わない。

    Raises:
        ValueError: 必須引数不備
        InsufficientCreditsError: 残高不足
        ReservationError: 内部エラー
    """
    if not session_id:
        raise ValueError("session_id is required")
    if not account_id:
        raise ValueError("account_id is required")
    if not client_request_id:
        raise ValueError("client_request_id is required")

    reservation_id = client_request_id

    existing = _get_existing(session_id, reservation_id)
    if existing is not None:
        return existing

    features = plan_to_features(plan)
    if not features:
        # 空 plan でも reservation doc は作る(0 credits)。finalize 呼び出しを監査可能にするため。
        reservation = _write_reservation_doc(
            session_id=session_id,
            reservation_id=reservation_id,
            account_id=account_id,
            uid=uid,
            plan=dict(plan or {}),
            operation_id=operation_id,
            items={},
            total_amount=0,
        )
        return reservation

    # 残高事前チェック(TOCTOU はあるが UX 改善目的)
    total_amount = sum(FEATURE_PRICE[f] for f in features)
    allowed, info = ai_credits.can_consume(account_id, total_amount)
    if not allowed:
        raise InsufficientCreditsError(
            f"insufficient credits: needed={total_amount} info={info}"
        )

    consumed: List[tuple[str, int, str]] = []  # (feature, amount, mode)
    items: Dict[str, Dict[str, Any]] = {}
    try:
        for feature in features:
            amount = FEATURE_PRICE[feature]
            mode = FEATURE_CREDIT_MODE.get(feature, "summary_generated")
            ai_credits.consume(account_id, amount, mode)
            consumed.append((feature, amount, mode))
            items[feature] = {
                "amount": amount,
                "mode": mode,
                "status": "reserved",
            }
    except Exception as exc:  # consume 失敗時はロールバック
        for feature, amount, mode in consumed:
            try:
                ai_credits.refund(account_id, amount, mode)
            except Exception:
                pass
        raise ReservationError(f"consume failed: {exc}") from exc

    return _write_reservation_doc(
        session_id=session_id,
        reservation_id=reservation_id,
        account_id=account_id,
        uid=uid,
        plan=dict(plan or {}),
        operation_id=operation_id,
        items=items,
        total_amount=total_amount,
    )


def _write_reservation_doc(
    *,
    session_id: str,
    reservation_id: str,
    account_id: str,
    uid: str,
    plan: Dict[str, Any],
    operation_id: str,
    items: Dict[str, Dict[str, Any]],
    total_amount: int,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "reservationId": reservation_id,
        "clientRequestId": reservation_id,
        "accountId": account_id,
        "uid": uid,
        "status": "reserved",
        "totalAmount": total_amount,
        "items": items,
        "derivedPlan": plan,
        "finalizeOperationId": operation_id,
        "createdAt": firestore.SERVER_TIMESTAMP,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }
    _res_ref(session_id, reservation_id).set(payload)
    result = dict(payload)
    # SERVER_TIMESTAMP sentinel は呼び出し側で扱いづらいので除去
    result["createdAt"] = None
    result["updatedAt"] = None
    return result


def commit(
    *,
    session_id: str,
    reservation_id: str,
    feature: Optional[str] = None,
) -> None:
    """feature(または全て)を committed にする。credits の移動はない。"""
    if not session_id or not reservation_id:
        raise ValueError("session_id and reservation_id are required")

    ref = _res_ref(session_id, reservation_id)
    snap = ref.get()
    if not snap.exists:
        raise ReservationError(f"reservation not found: {reservation_id}")

    data = snap.to_dict() or {}
    items: Dict[str, Dict[str, Any]] = dict(data.get("items") or {})

    if feature is not None:
        if feature not in items:
            raise ReservationError(f"feature not in reservation: {feature}")
        items[feature] = {**items[feature], "status": "committed"}
    else:
        for f, it in items.items():
            items[f] = {**it, "status": "committed"}

    all_committed = bool(items) and all(
        (it or {}).get("status") == "committed" for it in items.values()
    )

    update: Dict[str, Any] = {
        "items": items,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }
    if all_committed:
        update["status"] = "committed"
        update["committedAt"] = firestore.SERVER_TIMESTAMP
    ref.update(update)


def release(
    *,
    session_id: str,
    reservation_id: str,
    feature: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    """feature(または全ての pending feature)を release して credits を refund する。"""
    if not session_id or not reservation_id:
        raise ValueError("session_id and reservation_id are required")

    ref = _res_ref(session_id, reservation_id)
    snap = ref.get()
    if not snap.exists:
        raise ReservationError(f"reservation not found: {reservation_id}")

    data = snap.to_dict() or {}
    account_id = data.get("accountId")
    items: Dict[str, Dict[str, Any]] = dict(data.get("items") or {})

    targets = [feature] if feature is not None else list(items.keys())
    for f in targets:
        it = items.get(f)
        if not it:
            continue
        if it.get("status") != "reserved":
            continue
        amount = int(it.get("amount") or 0)
        mode = str(it.get("mode") or "summary_generated")
        if account_id and amount > 0:
            try:
                ai_credits.refund(account_id, amount, mode)
            except Exception:
                pass
        items[f] = {**it, "status": "released", "releaseReason": reason}

    any_reserved = any((it or {}).get("status") == "reserved" for it in items.values())
    update: Dict[str, Any] = {
        "items": items,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }
    if not any_reserved:
        update["status"] = "released"
        update["releasedAt"] = firestore.SERVER_TIMESTAMP
        if reason:
            update["releaseReason"] = reason
    ref.update(update)
