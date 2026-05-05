"""Unit tests for app.services.asset_delivery."""
from __future__ import annotations

import pytest

from app.services import asset_delivery, line_briefing


def test_returns_none_when_no_session(monkeypatch):
    monkeypatch.setattr(line_briefing, "get_latest_session", lambda acc: None)
    assert asset_delivery.get_latest_export_links("acct") is None


def test_returns_link_bundle_with_all_formats(monkeypatch):
    monkeypatch.setattr(line_briefing, "get_latest_session", lambda acc: {
        "id": "sid-1",
        "title": "今週の定例",
        "summary": "...",
    })
    monkeypatch.setenv("DEEPNOTE_APP_BASE_URL", "https://app.deepnote.example")
    bundle = asset_delivery.get_latest_export_links("acct")
    assert bundle["sessionId"] == "sid-1"
    assert bundle["title"] == "今週の定例"
    links = bundle["links"]
    assert links["web"] == "https://app.deepnote.example/sessions/sid-1"
    assert "format=pdf" in links["pdf"]
    assert "format=docx" in links["docx"]
    assert "format=pptx" in links["pptx"]


def test_falls_back_to_default_base_url(monkeypatch):
    monkeypatch.setattr(line_briefing, "get_latest_session", lambda acc: {
        "id": "sid", "title": "x", "summary": "",
    })
    monkeypatch.delenv("DEEPNOTE_APP_BASE_URL", raising=False)
    monkeypatch.delenv("CLOUD_RUN_SERVICE_URL", raising=False)
    bundle = asset_delivery.get_latest_export_links("acct")
    assert bundle["links"]["web"].startswith("https://")
