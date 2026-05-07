"""Phase 1.5 — group connect confirmation card + session picker UI.

Pure unit tests against the bubble builders and the postback handler's
guard logic. Heavier integration coverage (actual LINE webhook → reply)
is exercised manually via dev tag deploy + master smoke; these tests
just lock the contract pieces that are easy to regress.

Done-criteria mapping (from the user's spec):
  - 未連携ユーザーは接続不可                        — covered by handler check (separate test_line_webhook)
  - 連携済みユーザーは確認カード表示                 — bubble builder shape
  - confirm postbackでのみbinding active            — postback action vocabulary
  - cancelではbindingされない                       — cancel branch returns without create_group_link
  - 直近3〜5件の会議選択UIが出る                     — picker builder shape
  - 選択したsessionだけ共有される                    — postback data carries sid
  - 権限なし/削除済みsessionは共有されない            — covered by share_confirm guards
  - bot_audit に接続確認・選択・共有実行を記録        — assertion of audit calls
"""
from __future__ import annotations

from unittest.mock import patch

from app.routes import integrations_line


# ── Connect confirm card ─────────────────────────────────────────────

def test_connect_confirm_bubble_has_two_postback_buttons():
    bubble = integrations_line._build_group_connect_confirm_card(
        account_id_short="abcd1234", max_runs=30, max_paid=10,
        line_user_id="U" + "a" * 32,
    )
    assert bubble["type"] == "bubble"
    actions = [b["action"] for b in bubble["footer"]["contents"]
               if b.get("type") == "button"]
    assert len(actions) == 2
    data_strs = [a["data"] for a in actions]
    assert any(d.startswith("action=group_connect_confirm&u=") for d in data_strs)
    assert any(d.startswith("action=group_connect_cancel&u=") for d in data_strs)


def test_connect_confirm_card_warns_about_credit_consumption():
    bubble = integrations_line._build_group_connect_confirm_card(
        account_id_short="abcd1234", max_runs=30, max_paid=10,
        line_user_id="Ux",
    )
    body_text = " ".join(
        c.get("text", "") for c in bubble["body"]["contents"]
        if c.get("type") == "text"
    )
    # Card MUST tell the user about the consequences before they tap ✅.
    assert "クレジット" in body_text, "credit consumption warning missing"
    assert "議事録" in body_text or "セッション" in body_text, "session-access warning missing"
    assert "owner" in body_text or "オーナー" in body_text or "owner" in (
        bubble["header"]["contents"][0]["text"]
    )


def test_connect_confirm_card_encodes_intended_user_id():
    me = "U" + "1" * 32
    bubble = integrations_line._build_group_connect_confirm_card(
        account_id_short="abcd1234", max_runs=30, max_paid=10, line_user_id=me,
    )
    confirm_action = next(
        b["action"] for b in bubble["footer"]["contents"]
        if b.get("type") == "button" and "confirm" in b["action"]["data"]
    )
    assert me in confirm_action["data"], (
        "confirm postback must encode the intended u=<line_user_id> so "
        "the postback handler can verify the same user is tapping ✅"
    )


# ── Session picker ───────────────────────────────────────────────────

def test_session_picker_bubble_renders_3_to_5_rows():
    cands = [
        {"id": f"sess-{i}", "title": f"会議 {i}", "createdAt": None}
        for i in range(5)
    ]
    bubble = integrations_line._build_session_picker_bubble(cands, group_id="Gxxx")
    rows = bubble["body"]["contents"]
    assert len(rows) == 5
    # Each row should carry a share_confirm postback with the matching sid.
    for i, row in enumerate(rows):
        button = row["contents"][-1]
        assert button["type"] == "button"
        assert f"sid=sess-{i}" in button["action"]["data"]
        assert "dest=Gxxx" in button["action"]["data"]


def test_session_picker_caps_at_5_rows():
    cands = [
        {"id": f"sess-{i}", "title": f"会議 {i}", "createdAt": None}
        for i in range(8)
    ]
    bubble = integrations_line._build_session_picker_bubble(cands, group_id="Gxxx")
    assert len(bubble["body"]["contents"]) == 5


def test_session_picker_skips_empty_id():
    cands = [
        {"id": "sess-1", "title": "valid", "createdAt": None},
        {"id": "",        "title": "broken", "createdAt": None},
        {"id": "sess-3", "title": "valid 2", "createdAt": None},
    ]
    bubble = integrations_line._build_session_picker_bubble(cands, group_id="Gxxx")
    rows = bubble["body"]["contents"]
    assert len(rows) == 2
    sids = [r["contents"][-1]["action"]["data"] for r in rows]
    assert any("sid=sess-1" in s for s in sids)
    assert any("sid=sess-3" in s for s in sids)


def test_session_picker_handles_no_candidates_gracefully():
    bubble = integrations_line._build_session_picker_bubble([], group_id="Gxxx")
    # Falls back to a "not found" body row instead of an empty body.
    rows = bubble["body"]["contents"]
    assert len(rows) == 1
    text_field = rows[0].get("text") or " ".join(
        c.get("text", "") for c in rows[0].get("contents", [])
    )
    assert "見つかりません" in text_field


# ── Postback handler — confirm/cancel routing ────────────────────────

def _fake_event(*, action: str, line_user_id: str, group_id: str, intended_user: str = "") -> dict:
    u = intended_user or line_user_id
    return {
        "source": {"type": "group", "userId": line_user_id, "groupId": group_id},
        "replyToken": "rt-test",
        "postback": {"data": f"action={action}&u={u}"},
    }


def test_postback_cancel_does_not_create_group_link():
    ev = _fake_event(action="group_connect_cancel",
                     line_user_id="Ucancel", group_id="Gtest")
    with patch.object(integrations_line.line_messaging, "reply") as mock_reply, \
         patch.object(integrations_line.group_acl, "create_group_link") as mock_create, \
         patch.object(integrations_line.bot_audit, "record"):
        integrations_line._handle_postback_event(ev)
    mock_create.assert_not_called()
    assert mock_reply.called
    # Reply must be a "cancelled" message
    args, _ = mock_reply.call_args
    text = args[1][0]["text"]
    assert "キャンセル" in text


def test_postback_confirm_user_mismatch_blocks_create():
    """If the user pressing the button is NOT the original speaker, no link."""
    ev = _fake_event(action="group_connect_confirm",
                     line_user_id="Uimposter", group_id="Gtest",
                     intended_user="Uoriginal")
    with patch.object(integrations_line.line_messaging, "reply") as mock_reply, \
         patch.object(integrations_line.group_acl, "create_group_link") as mock_create, \
         patch.object(integrations_line.bot_audit, "record"):
        integrations_line._handle_postback_event(ev)
    mock_create.assert_not_called()
    args, _ = mock_reply.call_args
    assert "本人" in args[1][0]["text"]


def test_postback_confirm_unlinked_at_press_time_blocks_create():
    """If the requester revoked their DM link between card and tap, no link."""
    ev = _fake_event(action="group_connect_confirm",
                     line_user_id="Unolink", group_id="Gtest")
    with patch.object(integrations_line.line_link_tokens, "get_link",
                      return_value=None), \
         patch.object(integrations_line.line_messaging, "reply") as mock_reply, \
         patch.object(integrations_line.group_acl, "create_group_link") as mock_create, \
         patch.object(integrations_line.group_acl, "get_group_link",
                      return_value=None), \
         patch.object(integrations_line.bot_audit, "record"):
        integrations_line._handle_postback_event(ev)
    mock_create.assert_not_called()
    args, _ = mock_reply.call_args
    assert "連携" in args[1][0]["text"]


def test_postback_confirm_already_connected_blocks_create():
    """Race: someone else connected between card and tap."""
    ev = _fake_event(action="group_connect_confirm",
                     line_user_id="Uok", group_id="Gtest")
    with patch.object(integrations_line.line_link_tokens, "get_link",
                      return_value={"accountId": "acc-x", "deepnoteUid": "uid-x"}), \
         patch.object(integrations_line.group_acl, "get_group_link",
                      return_value={"ownerAccountId": "other"}), \
         patch.object(integrations_line.line_messaging, "reply") as mock_reply, \
         patch.object(integrations_line.group_acl, "create_group_link") as mock_create, \
         patch.object(integrations_line.bot_audit, "record"):
        integrations_line._handle_postback_event(ev)
    mock_create.assert_not_called()
    args, _ = mock_reply.call_args
    assert "既に接続" in args[1][0]["text"]


def test_postback_confirm_happy_path_creates_link():
    ev = _fake_event(action="group_connect_confirm",
                     line_user_id="Uok", group_id="Gtest")
    audit_calls = []
    def _audit(**kw):
        audit_calls.append(kw)
    with patch.object(integrations_line.line_link_tokens, "get_link",
                      return_value={"accountId": "acc-x", "deepnoteUid": "uid-x"}), \
         patch.object(integrations_line.group_acl, "get_group_link",
                      return_value=None), \
         patch.object(integrations_line.group_acl, "create_group_link") as mock_create, \
         patch.object(integrations_line.line_messaging, "reply") as mock_reply, \
         patch.object(integrations_line.bot_audit, "record", side_effect=_audit):
        integrations_line._handle_postback_event(ev)
    mock_create.assert_called_once()
    args, _ = mock_reply.call_args
    assert "接続しました" in args[1][0]["text"]
    # Audit must record a group_connect outcome=ok event.
    assert any(c.get("command") == "group_connect" and c.get("outcome") == "ok"
               for c in audit_calls)
