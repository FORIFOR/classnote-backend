"""
Comprehensive tests for Free and Standard plan limits.
Verifies that limits are correctly enforced and usage within limits is allowed.
"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, timezone

# Import limits for reference
from app.services.cost_guard import (
    FREE_LIMITS,
    BASIC_LIMITS,
    PREMIUM_LIMITS,
    _normalize_plan,
    _safe_dict,
    cost_guard,
)


class TestPlanLimitDefinitions:
    """Verify limit definitions are correct."""

    def test_free_limits_defined(self):
        """Free plan limits should be defined correctly."""
        assert FREE_LIMITS["cloud_stt_sec"] == 1800.0  # 30 mins
        assert FREE_LIMITS["cloud_sessions_started"] == 10
        assert FREE_LIMITS["summary_generated"] == 3
        assert FREE_LIMITS["quiz_generated"] == 3
        assert FREE_LIMITS["server_session"] == 5

    def test_basic_limits_defined(self):
        """Basic/Standard plan limits should be defined correctly."""
        assert BASIC_LIMITS["cloud_stt_sec"] == 7200.0  # 120 mins
        assert BASIC_LIMITS["cloud_sessions_started"] == 100
        assert BASIC_LIMITS["summary_generated"] == 100
        assert BASIC_LIMITS["quiz_generated"] == 100
        assert BASIC_LIMITS["server_session"] == 300

    def test_premium_limits_defined(self):
        """Premium plan limits should be defined correctly."""
        assert PREMIUM_LIMITS["cloud_stt_sec"] == 7200.0  # Per session, no monthly cap
        assert PREMIUM_LIMITS["llm_calls"] == 1000
        assert PREMIUM_LIMITS["server_session"] == 300


class TestPlanNormalization:
    """Test plan name normalization."""

    def test_normalize_free_variations(self):
        """Various free plan names should normalize to 'free'."""
        assert _normalize_plan("free") == "free"
        assert _normalize_plan("") == "free"
        assert _normalize_plan(None) == "free"
        assert _normalize_plan("unknown") == "free"

    def test_normalize_basic_variations(self):
        """'basic' and 'standard' should normalize to 'basic'."""
        assert _normalize_plan("basic") == "basic"
        assert _normalize_plan("standard") == "basic"

    def test_normalize_premium_variations(self):
        """'premium' and 'pro' should normalize to 'premium'."""
        assert _normalize_plan("premium") == "premium"
        assert _normalize_plan("pro") == "premium"


class TestFreePlanLimitEnforcement:
    """Test that free plan limits are correctly enforced."""

    @patch("app.services.cost_guard.db")
    def test_free_plan_cloud_stt_within_limit(self, mock_db):
        """Free user with usage within limit should be allowed."""
        # Setup: Free user with 1000 seconds used (limit 1800)
        mock_user_doc = MagicMock()
        mock_user_doc.exists = True
        mock_user_doc.to_dict.return_value = {"plan": "free"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {"cloud_stt_sec": 1000.0}

        mock_db.collection.return_value.document.return_value.get.return_value = mock_user_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        import asyncio
        result = asyncio.run(cost_guard.get_usage_report("test_uid", mode="user"))

        assert result["plan"] == "free"
        assert result["canStart"] == True
        assert result["usedSeconds"] == 1000.0
        assert result["remainingSeconds"] == 800.0  # 1800 - 1000
        assert result["reasonIfBlocked"] is None

    @patch("app.services.cost_guard.db")
    def test_free_plan_cloud_stt_at_limit(self, mock_db):
        """Free user at exact limit should be blocked."""
        mock_user_doc = MagicMock()
        mock_user_doc.exists = True
        mock_user_doc.to_dict.return_value = {"plan": "free"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {"cloud_stt_sec": 1800.0}

        mock_db.collection.return_value.document.return_value.get.return_value = mock_user_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        import asyncio
        result = asyncio.run(cost_guard.get_usage_report("test_uid", mode="user"))

        assert result["canStart"] == False
        assert result["reasonIfBlocked"] == "cloud_minutes_limit"
        assert result["remainingSeconds"] == 0.0

    @patch("app.services.cost_guard.db")
    def test_free_plan_sessions_at_limit(self, mock_db):
        """Free user at session limit should be blocked."""
        mock_user_doc = MagicMock()
        mock_user_doc.exists = True
        mock_user_doc.to_dict.return_value = {"plan": "free"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {
            "cloud_stt_sec": 500.0,  # Within limit
            "cloud_sessions_started": 10  # At limit
        }

        mock_db.collection.return_value.document.return_value.get.return_value = mock_user_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        import asyncio
        result = asyncio.run(cost_guard.get_usage_report("test_uid", mode="user"))

        assert result["canStart"] == False
        assert result["reasonIfBlocked"] == "cloud_session_limit"


class TestBasicPlanLimitEnforcement:
    """Test that basic/standard plan limits are correctly enforced."""

    @patch("app.services.cost_guard.db")
    def test_standard_plan_cloud_stt_within_limit(self, mock_db):
        """Standard user with usage within limit should be allowed."""
        mock_user_doc = MagicMock()
        mock_user_doc.exists = True
        mock_user_doc.to_dict.return_value = {"plan": "standard"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {"cloud_stt_sec": 3600.0}  # 60 mins used

        mock_db.collection.return_value.document.return_value.get.return_value = mock_user_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        import asyncio
        result = asyncio.run(cost_guard.get_usage_report("test_uid", mode="user"))

        assert result["plan"] == "basic"  # Normalized
        assert result["canStart"] == True
        assert result["limitSeconds"] == 7200.0
        assert result["remainingSeconds"] == 3600.0  # 7200 - 3600

    @patch("app.services.cost_guard.db")
    def test_standard_plan_cloud_stt_at_limit(self, mock_db):
        """Standard user at exact limit should be blocked."""
        mock_user_doc = MagicMock()
        mock_user_doc.exists = True
        mock_user_doc.to_dict.return_value = {"plan": "standard"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {"cloud_stt_sec": 7200.0}  # Full 120 mins

        mock_db.collection.return_value.document.return_value.get.return_value = mock_user_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        import asyncio
        result = asyncio.run(cost_guard.get_usage_report("test_uid", mode="user"))

        assert result["plan"] == "basic"
        assert result["canStart"] == False
        assert result["reasonIfBlocked"] == "cloud_minutes_limit"

    @patch("app.services.cost_guard.db")
    def test_basic_plan_higher_limits_than_free(self, mock_db):
        """Basic plan should have higher limits than free."""
        mock_user_doc = MagicMock()
        mock_user_doc.exists = True
        mock_user_doc.to_dict.return_value = {"plan": "basic"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {
            "cloud_stt_sec": 2000.0,  # Would exceed free limit (1800)
            "cloud_sessions_started": 15  # Would exceed free limit (10)
        }

        mock_db.collection.return_value.document.return_value.get.return_value = mock_user_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        import asyncio
        result = asyncio.run(cost_guard.get_usage_report("test_uid", mode="user"))

        # Should still be allowed under basic limits
        assert result["canStart"] == True
        assert result["limitSeconds"] == 7200.0
        assert result["sessionLimit"] == 100


class TestGuardCanConsumeLogic:
    """Test the transactional guard_can_consume logic."""

    @patch("app.services.cost_guard.db")
    def test_guard_allows_consumption_within_limit(self, mock_db):
        """Guard should allow consumption when within limits."""
        # Setup transaction mock
        mock_transaction = MagicMock()

        mock_user_doc = MagicMock()
        mock_user_doc.exists = True
        mock_user_doc.to_dict.return_value = {"plan": "free"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {"summary_generated": 1}  # 1 used, limit 3

        # Test the internal logic directly
        result = cost_guard._check_and_reserve_logic(
            mock_transaction,
            MagicMock(),  # u_ref
            MagicMock(),  # m_ref
            "test_uid",
            "summary_generated",
            1,  # amount
            "2026-01",
            u_snap=mock_user_doc,
            m_snap=mock_monthly_doc
        )

        allowed, meta = result
        assert allowed == True
        assert meta is None

    @patch("app.services.cost_guard.db")
    def test_guard_blocks_consumption_at_limit(self, mock_db):
        """Guard should block consumption when at limit."""
        mock_transaction = MagicMock()

        mock_user_doc = MagicMock()
        mock_user_doc.exists = True
        mock_user_doc.to_dict.return_value = {"plan": "free"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {"summary_generated": 3}  # At limit

        result = cost_guard._check_and_reserve_logic(
            mock_transaction,
            MagicMock(),
            MagicMock(),
            "test_uid",
            "summary_generated",
            1,
            "2026-01",
            u_snap=mock_user_doc,
            m_snap=mock_monthly_doc
        )

        allowed, meta = result
        assert allowed == False
        assert meta["rule"] == "summary_generated_limit"
        assert meta["limit"] == 3
        assert meta["used"] == 3

    @patch("app.services.cost_guard.db")
    def test_guard_boundary_last_allowed(self, mock_db):
        """Guard should allow the last unit before hitting limit."""
        mock_transaction = MagicMock()

        mock_user_doc = MagicMock()
        mock_user_doc.exists = True
        mock_user_doc.to_dict.return_value = {"plan": "free"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {"quiz_generated": 2}  # 2 used, limit 3

        result = cost_guard._check_and_reserve_logic(
            mock_transaction,
            MagicMock(),
            MagicMock(),
            "test_uid",
            "quiz_generated",
            1,  # This should bring it to 3, which equals limit
            "2026-01",
            u_snap=mock_user_doc,
            m_snap=mock_monthly_doc
        )

        allowed, meta = result
        # 2 + 1 = 3, which is NOT > 3, so it should be allowed
        assert allowed == True


class TestPremiumPlanNoLimits:
    """Test that premium plan has effectively no limits for cloud features."""

    @patch("app.services.cost_guard.db")
    def test_premium_plan_high_usage_allowed(self, mock_db):
        """Premium user with high usage should still be allowed."""
        mock_user_doc = MagicMock()
        mock_user_doc.exists = True
        mock_user_doc.to_dict.return_value = {"plan": "premium"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {
            "cloud_stt_sec": 50000.0,  # Way above any limit
            "cloud_sessions_started": 500
        }

        mock_db.collection.return_value.document.return_value.get.return_value = mock_user_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        import asyncio
        result = asyncio.run(cost_guard.get_usage_report("test_uid", mode="user"))

        assert result["plan"] == "premium"
        assert result["canStart"] == True  # Premium never blocked


class TestAccountVsUserMode:
    """Test that mode parameter correctly selects collection."""

    @patch("app.services.cost_guard.db")
    def test_mode_account_uses_accounts_collection(self, mock_db):
        """mode='account' should look up accounts collection."""
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = {"plan": "basic"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {}

        mock_db.collection.return_value.document.return_value.get.return_value = mock_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        import asyncio
        asyncio.run(cost_guard.get_usage_report("acc_123", mode="account"))

        # Verify accounts collection was accessed
        mock_db.collection.assert_any_call("accounts")

    @patch("app.services.cost_guard.db")
    def test_mode_user_uses_users_collection(self, mock_db):
        """mode='user' should look up users collection."""
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = {"plan": "free"}

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = True
        mock_monthly_doc.to_dict.return_value = {}

        mock_db.collection.return_value.document.return_value.get.return_value = mock_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        import asyncio
        asyncio.run(cost_guard.get_usage_report("uid_123", mode="user"))

        # Verify users collection was accessed
        mock_db.collection.assert_any_call("users")


class TestNewUserDefaults:
    """Test that new users get free tier defaults."""

    @patch("app.services.cost_guard.db")
    def test_nonexistent_user_gets_free_defaults(self, mock_db):
        """User not in database should get free tier limits."""
        mock_doc = MagicMock()
        mock_doc.exists = False  # User doesn't exist

        mock_monthly_doc = MagicMock()
        mock_monthly_doc.exists = False  # No usage yet

        mock_db.collection.return_value.document.return_value.get.return_value = mock_doc
        mock_db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = mock_monthly_doc

        import asyncio
        result = asyncio.run(cost_guard.get_usage_report("new_user", mode="user"))

        assert result["plan"] == "free"
        assert result["canStart"] == True
        assert result["limitSeconds"] == FREE_LIMITS["cloud_stt_sec"]
        assert result["sessionLimit"] == FREE_LIMITS["cloud_sessions_started"]
        assert result["usedSeconds"] == 0.0
        assert result["sessionsStarted"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
