"""
finalize.py - finalize v2 のオーケストレーション層。

責務:
  - canonical import 完了の確定(`import_state.mark_import_completed`)
  - derived plan の解決(legacy フォールバック含む)
  - credits 予約(`credits_reservations.reserve`)
  - per-feature worker 投入(`task_queue.enqueue_derived_finalize_task`)
  - audit 発行 / session.finalize mirror フィールド更新
  - clientRequestId ベースの冪等化

route handler は session 解決・権限チェック・transcript 再構築までを行い、
本関数に前提情報を渡す(I/O 分離のため)。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from google.cloud import firestore

from app.firebase import db
from app.services import audit, credits_reservations, project_folders
from app.services.finalize_helpers import DerivedPlan, plan_to_features, price
from app.services.import_state import mark_import_completed
from app.task_queue import enqueue_derived_finalize_task


logger = logging.getLogger(__name__)

_SESSIONS = "sessions"
_DEFAULT_PLAN: DerivedPlan = {"summary": True}


class FinalizeError(Exception):
    """finalize 処理全般の失敗。"""


class ChunkCountMismatchError(FinalizeError):
    """expectedChunkCount と server canonical chunk_count が不一致。"""


def resolve_derived_plan(
    derived_plan: Optional[Dict[str, Any]],
    *,
    legacy_generate_summary: bool = True,
    legacy_generate_playlist: bool = False,
    legacy_generate_quiz: bool = False,
) -> DerivedPlan:
    """request body から derivedPlan を決定する。

    優先順位:
      1. body.derivedPlan が明示指定されていればそのまま使う
      2. そうでなければ legacy generateSummary/Quiz フラグから構築
      3. 何も指定されていなければ `_DEFAULT_PLAN`({summary: True})
    """
    if derived_plan is not None:
        plan: DerivedPlan = {}
        for key in ("summary", "highlights", "quiz"):
            if derived_plan.get(key):
                plan[key] = True  # type: ignore[literal-required]
        if plan:
            return plan
        return {}

    legacy_plan: DerivedPlan = {}
    if legacy_generate_summary:
        legacy_plan["summary"] = True
    if legacy_generate_quiz:
        legacy_plan["quiz"] = True
    # legacy_generate_playlist は derivedPlan に乗らない(別ジョブ扱い)
    if legacy_plan:
        return legacy_plan
    return dict(_DEFAULT_PLAN)


def _new_operation_id() -> str:
    return f"fin_{uuid.uuid4().hex[:16]}"


def _normalize_client_request_id(client_request_id: Optional[str], session_id: str) -> str:
    """clientRequestId が未指定なら session_id ベースの決定的 id を生成。

    iOS 既存クライアントは clientRequestId を送らないため、session_id 単位で
    「1 session 1 reservation」になるようにフォールバックする。
    """
    if client_request_id and client_request_id.strip():
        return client_request_id.strip()
    return f"finalize:{session_id}"


def finalize_session_v2(
    *,
    session_id: str,
    session_data: Dict[str, Any],
    uid: str,
    account_id: str,
    transcript_text: Optional[str],
    chunk_count: int,
    last_chunk_index: Optional[int],
    import_source: str,
    derived_plan_req: Optional[Dict[str, Any]],
    client_request_id: Optional[str],
    expected_chunk_count: Optional[int],
    legacy_generate_summary: bool = True,
    legacy_generate_playlist: bool = False,
    legacy_generate_quiz: bool = False,
) -> Dict[str, Any]:
    """finalize v2 本体(同期版; route handler から asyncio.to_thread で呼ぶ)。

    冪等化:
      - 正規化した client_request_id が既存 reservation にヒットすれば、
        既存結果を返し副作用なしで終了する。
      - それ以外は canonical 確定 → reserve → enqueue → audit の順に実行。

    失敗ハンドリング:
      - expectedChunkCount 不一致 → `ChunkCountMismatchError`(credits 未消費)
      - enqueue が途中で失敗 → reservation を release し `FinalizeError`

    Returns:
        dict: {sessionId, status, reservationId, operationId, derivedPlan,
               features, credits, importState, idempotent}
    """
    doc_ref = db.collection(_SESSIONS).document(session_id)

    normalized_req_id = _normalize_client_request_id(client_request_id, session_id)
    operation_id = _new_operation_id()

    # Idempotency: 既存 reservation の再利用。
    existing = credits_reservations._get_existing(session_id, normalized_req_id)  # noqa: SLF001
    if existing is not None:
        audit.emit(
            "session.finalize.replayed",
            session_id=session_id,
            uid=uid,
            operation_id=operation_id,
            request_id=normalized_req_id,
            reservationId=normalized_req_id,
        )
        return _build_response(
            session_id=session_id,
            reservation=existing,
            operation_id=existing.get("finalizeOperationId") or operation_id,
            chunk_count=chunk_count,
            idempotent=True,
        )

    # expectedChunkCount check(client optional; 指定されれば厳格チェック)
    if expected_chunk_count is not None and int(expected_chunk_count) != int(chunk_count):
        audit.emit(
            "session.finalize.chunk_mismatch",
            severity="WARN",
            session_id=session_id,
            uid=uid,
            operation_id=operation_id,
            request_id=normalized_req_id,
            expectedChunkCount=int(expected_chunk_count),
            serverChunkCount=int(chunk_count),
        )
        raise ChunkCountMismatchError(
            f"expected={expected_chunk_count} server={chunk_count}"
        )

    plan = resolve_derived_plan(
        derived_plan_req,
        legacy_generate_summary=legacy_generate_summary,
        legacy_generate_playlist=legacy_generate_playlist,
        legacy_generate_quiz=legacy_generate_quiz,
    )
    features = plan_to_features(plan)
    price_map = price(plan)

    audit.emit(
        "session.finalize.requested",
        session_id=session_id,
        uid=uid,
        operation_id=operation_id,
        request_id=normalized_req_id,
        derivedPlan=dict(plan),
        expectedChunkCount=expected_chunk_count,
        serverChunkCount=int(chunk_count),
    )

    # Step 1: canonical 確定(mark_import_completed)
    try:
        mark_import_completed(
            session_id,
            chunk_count=int(chunk_count),
            last_chunk_index=last_chunk_index,
            source=import_source,  # type: ignore[arg-type]
        )
    except Exception as exc:
        audit.emit(
            "import_state.mark_completed_failed",
            severity="ERROR",
            session_id=session_id,
            uid=uid,
            operation_id=operation_id,
            request_id=normalized_req_id,
            error=str(exc),
        )
        raise FinalizeError(f"mark_import_completed failed: {exc}") from exc

    # Step 2: credits reserve
    try:
        reservation = credits_reservations.reserve(
            session_id=session_id,
            account_id=account_id,
            uid=uid,
            plan=plan,
            client_request_id=normalized_req_id,
            operation_id=operation_id,
        )
    except credits_reservations.InsufficientCreditsError as exc:
        audit.emit(
            "credits.reserve_blocked",
            severity="WARN",
            session_id=session_id,
            uid=uid,
            operation_id=operation_id,
            request_id=normalized_req_id,
            reason="insufficient",
            detail=str(exc),
        )
        raise
    except Exception as exc:
        audit.emit(
            "credits.reserve_failed",
            severity="ERROR",
            session_id=session_id,
            uid=uid,
            operation_id=operation_id,
            request_id=normalized_req_id,
            error=str(exc),
        )
        raise FinalizeError(f"credits reserve failed: {exc}") from exc

    audit.emit(
        "credits.reserved",
        session_id=session_id,
        uid=uid,
        operation_id=operation_id,
        request_id=normalized_req_id,
        reservationId=normalized_req_id,
        totalAmount=reservation.get("totalAmount"),
        items=price_map,
    )

    # Step 3: per-feature worker enqueue(best-effort; 失敗は release)
    enqueue_errors: list[str] = []
    for feature in features:
        try:
            enqueue_derived_finalize_task(
                session_id,
                feature,
                reservation_id=normalized_req_id,
                user_id=uid,
                account_id=account_id,
                operation_id=operation_id,
                client_request_id=normalized_req_id,
            )
        except Exception as exc:
            logger.exception(
                f"[finalize_v2] enqueue failed session={session_id} feature={feature}"
            )
            enqueue_errors.append(f"{feature}: {exc}")

    if enqueue_errors and len(enqueue_errors) == len(features):
        # 全滅 → reservation を release してエラー
        try:
            credits_reservations.release(
                session_id=session_id,
                reservation_id=normalized_req_id,
                reason="enqueue_failed",
            )
        except Exception:
            logger.exception("[finalize_v2] release after enqueue failure also failed")
        audit.emit(
            "session.finalize.enqueue_failed",
            severity="ERROR",
            session_id=session_id,
            uid=uid,
            operation_id=operation_id,
            request_id=normalized_req_id,
            errors=enqueue_errors,
        )
        raise FinalizeError(f"all derived enqueue failed: {enqueue_errors}")

    # Step 4: session.finalize mirror fields
    try:
        doc_ref.update({
            "status": "final",
            "finalize.reservationId": normalized_req_id,
            "finalize.operationId": operation_id,
            "finalize.lastRequestAt": firestore.SERVER_TIMESTAMP,
            "finalize.derivedPlan": dict(plan),
            "updatedAt": firestore.SERVER_TIMESTAMP,
        })
    except Exception as exc:
        logger.warning(f"[finalize_v2] session.finalize mirror update failed: {exc}")

    # Step 4.5: project/folder 配下の session 参照を final 状態で更新
    try:
        latest_session_data = dict(session_data or {})
        latest_session_data["status"] = "final"
        project_folders.refresh_session_organization_on_finalize(
            uid=uid,
            session_id=session_id,
            session_data=latest_session_data,
            account_id=account_id,
        )
    except Exception as exc:
        logger.warning(f"[finalize_v2] organization refresh skipped: {exc}")

    audit.emit(
        "session.finalize.enqueued",
        session_id=session_id,
        uid=uid,
        operation_id=operation_id,
        request_id=normalized_req_id,
        reservationId=normalized_req_id,
        features=features,
        partialEnqueueErrors=enqueue_errors or None,
    )

    return _build_response(
        session_id=session_id,
        reservation=reservation,
        operation_id=operation_id,
        chunk_count=chunk_count,
        idempotent=False,
        has_transcript=bool(transcript_text),
        transcript_len=len(transcript_text) if transcript_text else 0,
        partial_enqueue_errors=enqueue_errors or None,
    )


def _build_response(
    *,
    session_id: str,
    reservation: Dict[str, Any],
    operation_id: str,
    chunk_count: int,
    idempotent: bool,
    has_transcript: bool = True,
    transcript_len: int = 0,
    partial_enqueue_errors: Optional[list[str]] = None,
) -> Dict[str, Any]:
    plan = reservation.get("derivedPlan") or {}
    items = reservation.get("items") or {}
    return {
        "ok": True,
        "sessionId": session_id,
        "status": "final",
        "reservationId": reservation.get("reservationId"),
        "operationId": operation_id,
        "derivedPlan": plan,
        "features": list(items.keys()),
        "credits": {
            "totalAmount": reservation.get("totalAmount", 0),
            "items": items,
        },
        "importState": {
            "completed": True,
            "chunkCount": int(chunk_count),
        },
        "idempotent": idempotent,
        "hasTranscript": has_transcript,
        "transcriptTextLen": transcript_len,
        "partialEnqueueErrors": partial_enqueue_errors,
    }
