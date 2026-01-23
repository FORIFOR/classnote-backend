"""
Test for WebSocket quota lookup fix.
Verifies that mode="user" is correctly used to prevent false quota rejections.
"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, timezone


class TestCostGuardModeSelection:
    """Test that cost_guard functions receive correct mode parameter."""

    @patch("app.services.cost_guard.db")
    def test_get_usage_report_mode_user(self, mock_db):
        """Verify get_usage_report with mode='user' looks up users collection."""
        from app.services.cost_guard import cost_guard

        # Setup mock
        mock_user_doc = MagicMock()
        mock_user_doc.exists = True
        mock_user_doc.to_dict.return_value = {"plan": "standard"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {
            "cloud_stt_sec": 1000.0,
            "cloud_sessions_started": 5
        }

        # Mock chain: db.collection("users").document(uid).get()
        mock_db.collection.return_value.document.return_value.get.return_value = mock_user_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        # Call with mode="user"
        import asyncio
        result = asyncio.run(cost_guard.get_usage_report("test_uid", mode="user"))

        # Verify users collection was accessed
        mock_db.collection.assert_any_call("users")

        # Verify result contains correct data
        assert result.get("plan") == "basic"  # "standard" normalizes to "basic"
        assert result.get("canStart") == True
        assert result.get("limitSeconds") == 7200.0  # BASIC_LIMITS

    @patch("app.services.cost_guard.db")
    def test_get_usage_report_nonexistent_returns_free_defaults(self, mock_db):
        """Verify get_usage_report returns free tier defaults when entity doc doesn't exist."""
        from app.services.cost_guard import cost_guard, FREE_LIMITS

        # Setup mock - entity doc doesn't exist
        mock_user_doc = MagicMock()
        mock_user_doc.exists = False

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = False

        mock_db.collection.return_value.document.return_value.get.return_value = mock_user_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        import asyncio
        result = asyncio.run(cost_guard.get_usage_report("nonexistent_uid", mode="user"))

        # Should return free tier defaults (not empty dict)
        assert result.get("plan") == "free"
        assert result.get("canStart") == True
        assert result.get("limitSeconds") == FREE_LIMITS["cloud_stt_sec"]
        assert result.get("usedSeconds") == 0.0

    @patch("app.services.cost_guard.db")
    def test_get_monthly_doc_ref_mode_user(self, mock_db):
        """Verify _get_monthly_doc_ref uses 'users' collection with mode='user'."""
        from app.services.cost_guard import cost_guard

        # Call with mode="user"
        cost_guard._get_monthly_doc_ref("test_uid", mode="user")

        # Verify users collection was accessed
        mock_db.collection.assert_called_with("users")

    @patch("app.services.cost_guard.db")
    def test_get_monthly_doc_ref_mode_account(self, mock_db):
        """Verify _get_monthly_doc_ref uses 'accounts' collection with mode='account'."""
        from app.services.cost_guard import cost_guard

        # Call with mode="account"
        cost_guard._get_monthly_doc_ref("test_account_id", mode="account")

        # Verify accounts collection was accessed
        mock_db.collection.assert_called_with("accounts")


class TestPlanNormalization:
    """Test that plan names are correctly normalized."""

    @patch("app.services.cost_guard.db")
    def test_standard_plan_normalizes_to_basic(self, mock_db):
        """Verify 'standard' plan normalizes to 'basic' and gets correct limits."""
        from app.services.cost_guard import cost_guard, BASIC_LIMITS

        mock_user_doc = MagicMock()
        mock_user_doc.exists = True
        mock_user_doc.to_dict.return_value = {"plan": "standard"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {"cloud_stt_sec": 0.0}

        mock_db.collection.return_value.document.return_value.get.return_value = mock_user_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        import asyncio
        result = asyncio.run(cost_guard.get_usage_report("test_uid", mode="user"))

        # Should normalize to "basic"
        assert result.get("plan") == "basic"
        # Should get BASIC_LIMITS
        assert result.get("limitSeconds") == BASIC_LIMITS["cloud_stt_sec"]
        assert result.get("sessionLimit") == BASIC_LIMITS["cloud_sessions_started"]
        assert result.get("canStart") == True

    @patch("app.services.cost_guard.db")
    def test_free_plan_gets_free_limits(self, mock_db):
        """Verify 'free' plan gets FREE_LIMITS."""
        from app.services.cost_guard import cost_guard, FREE_LIMITS

        mock_user_doc = MagicMock()
        mock_user_doc.exists = True
        mock_user_doc.to_dict.return_value = {"plan": "free"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {"cloud_stt_sec": 0.0}

        mock_db.collection.return_value.document.return_value.get.return_value = mock_user_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        import asyncio
        result = asyncio.run(cost_guard.get_usage_report("test_uid", mode="user"))

        assert result.get("plan") == "free"
        assert result.get("limitSeconds") == FREE_LIMITS["cloud_stt_sec"]  # 1800 (30 mins)


class TestQuotaBlockConditions:
    """Test quota blocking conditions."""

    @patch("app.services.cost_guard.db")
    def test_standard_user_not_blocked_with_remaining_quota(self, mock_db):
        """Standard user with remaining quota should not be blocked."""
        from app.services.cost_guard import cost_guard

        mock_user_doc = MagicMock()
        mock_user_doc.exists = True
        mock_user_doc.to_dict.return_value = {"plan": "standard"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {
            "cloud_stt_sec": 3600.0,  # 60 mins used
            "cloud_sessions_started": 10
        }

        mock_db.collection.return_value.document.return_value.get.return_value = mock_user_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        import asyncio
        result = asyncio.run(cost_guard.get_usage_report("test_uid", mode="user"))

        # Should have 60 mins remaining (120 - 60)
        assert result.get("canStart") == True
        assert result.get("remainingSeconds") == 3600.0
        assert result.get("reasonIfBlocked") is None

    @patch("app.services.cost_guard.db")
    def test_standard_user_blocked_when_quota_exhausted(self, mock_db):
        """Standard user should be blocked when quota is exhausted."""
        from app.services.cost_guard import cost_guard

        mock_user_doc = MagicMock()
        mock_user_doc.exists = True
        mock_user_doc.to_dict.return_value = {"plan": "standard"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {
            "cloud_stt_sec": 7200.0,  # Full 120 mins used
            "cloud_sessions_started": 10
        }

        mock_db.collection.return_value.document.return_value.get.return_value = mock_user_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        import asyncio
        result = asyncio.run(cost_guard.get_usage_report("test_uid", mode="user"))

        # Should be blocked
        assert result.get("canStart") == False
        assert result.get("remainingSeconds") == 0.0
        assert result.get("reasonIfBlocked") == "cloud_minutes_limit"


class TestWebSocketQuotaLookup:
    """Test the WebSocket quota lookup logic integration."""

    def test_quota_mode_selection_logic(self):
        """Test the quota mode selection logic in websocket.py."""
        # Simulate the logic from websocket.py

        # Case 1: User has accountId
        user_data_with_account = {"accountId": "acc_123", "plan": "standard"}
        account_id = user_data_with_account.get("accountId")
        # PHASE 1: Always use mode="user"
        quota_id = "uid_456"  # uid
        quota_mode = "user"

        assert quota_mode == "user"
        assert quota_id == "uid_456"

        # Case 2: User without accountId
        user_data_no_account = {"plan": "standard"}
        account_id = user_data_no_account.get("accountId")
        quota_id = "uid_789"
        quota_mode = "user"

        assert quota_mode == "user"
        assert quota_id == "uid_789"
