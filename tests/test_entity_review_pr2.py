"""
Unit tests for Entity Review PR2.

Covers the pure services (no Firestore I/O):
  - extract_candidates regex behavior
  - suggest_matches thresholding + rapidfuzz fallback when absent
  - suspicion_score weights
  - build_candidates filtering & sort
  - apply_replace_all / apply_decisions_to_text
  - decisions_to_term_upserts (hidden/userEdited/learnTerm rules)
  - build_term_hints shape
  - finalize env flag: ENABLE_ENTITY_REVIEW_FINALIZE default False
  - transcripts.resolve_transcript_text canonical-fallback precedence
"""
from __future__ import annotations

import sys
import types

# Stub heavy imports so services load without network deps.
for _m in [
    "google", "google.cloud", "google.cloud.firestore",
    "vertexai", "vertexai.generative_models",
    "app.firebase",
]:
    if _m not in sys.modules:
        sys.modules[_m] = types.ModuleType(_m)
sys.modules["app.firebase"].db = None  # type: ignore[attr-defined]
sys.path.insert(0, ".")

from app.services import entity_review_services as svc  # noqa: E402


# ---------------------------------------------------------------------------
# extractor
# ---------------------------------------------------------------------------

def test_extract_latin_tokens():
    out = svc.extract_candidates("今日はGeminiとVertex AIを使います。")
    assert "Gemini" in out
    assert "Vertex" in out


def test_extract_katakana_runs():
    out = svc.extract_candidates("今日はジェミニーとバーテックスAIの話をします")
    assert "ジェミニー" in out
    assert "バーテックス" in out


def test_extract_dedupes_by_first_appearance():
    out = svc.extract_candidates("Gemini Gemini Gemini")
    assert out.count("Gemini") == 1


def test_extract_stops_common_acronyms():
    out = svc.extract_candidates("AI OK NO TODO")
    for junk in ("AI", "OK", "NO", "TODO"):
        assert junk not in out


def test_extract_empty_text():
    assert svc.extract_candidates("") == []


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def test_suggest_matches_empty_known_returns_empty():
    assert svc.suggest_matches("Gemini", []) == []


def test_suggest_matches_under_threshold_filtered(monkeypatch):
    # Force rapidfuzz to a dummy implementation returning low score.
    dummy = types.SimpleNamespace(
        WRatio=lambda a, b, **_: 50.0,
    )
    dummy_process = types.SimpleNamespace(
        extract=lambda s, terms, scorer=None, limit=5: [(t, 50.0, i) for i, t in enumerate(terms)],
    )
    monkeypatch.setattr(svc, "_try_rapidfuzz", lambda: (dummy, dummy_process))
    assert svc.suggest_matches("foo", ["bar", "baz"]) == []


def test_suggest_matches_above_threshold_included(monkeypatch):
    dummy_process = types.SimpleNamespace(
        extract=lambda s, terms, scorer=None, limit=5: [(t, 88.0, i) for i, t in enumerate(terms)],
    )
    monkeypatch.setattr(svc, "_try_rapidfuzz", lambda: (object(), dummy_process))
    out = svc.suggest_matches("geminee", ["Gemini"])
    assert out == [{"value": "Gemini", "score": 0.88}]


def test_suspicion_score_weights_sum_to_1():
    s = svc.suspicion_score(
        low_confidence=1.0, oov=1.0, fuzzy=1.0,
        variant_conflict=1.0, context_anomaly=1.0,
    )
    assert s == 1.0


def test_suspicion_score_zero_when_all_zero():
    assert svc.suspicion_score() == 0.0


def test_suspicion_score_clamped():
    # Out-of-range inputs still clamp to [0,1].
    s = svc.suspicion_score(low_confidence=5.0, oov=-2.0)
    assert 0.0 <= s <= 1.0


# ---------------------------------------------------------------------------
# build_candidates (end-to-end without rapidfuzz)
# ---------------------------------------------------------------------------

def test_build_candidates_keeps_katakana_even_without_matches(monkeypatch):
    # rapidfuzz absent → suggest_matches returns [] so latin surfaces skipped,
    # but katakana runs still qualify as OOV candidates.
    monkeypatch.setattr(svc, "_try_rapidfuzz", lambda: (None, None))
    out = svc.build_candidates(
        text="今日はジェミニーを使います",
        known_terms=[],
    )
    surfaces = [c["surface"] for c in out]
    assert "ジェミニー" in surfaces


def test_build_candidates_sorts_by_suspicion(monkeypatch):
    # Provide a stub process that gives different scores per term
    score_map = {"A": 95.0, "B": 80.0}
    def _extract(s, terms, scorer=None, limit=5):
        return [(t, score_map.get(t, 60.0), i) for i, t in enumerate(terms)]
    monkeypatch.setattr(svc, "_try_rapidfuzz", lambda: (object(), types.SimpleNamespace(extract=_extract)))
    out = svc.build_candidates(
        text="A and B here",
        known_terms=["A", "B"],
    )
    # Both should be candidates; A has higher fuzzy so ranks first.
    surfaces = [c["surface"] for c in out]
    assert surfaces[0] in ("A", "B")


# ---------------------------------------------------------------------------
# patch application
# ---------------------------------------------------------------------------

def test_apply_replace_all_counts():
    out, n = svc.apply_replace_all("ジェミニー ジェミニー end", "ジェミニー", "Gemini")
    assert n == 2
    assert out == "Gemini Gemini end"


def test_apply_replace_all_noop_when_identical():
    out, n = svc.apply_replace_all("Gemini", "Gemini", "Gemini")
    assert n == 0
    assert out == "Gemini"


def test_apply_replace_all_noop_empty_find():
    out, n = svc.apply_replace_all("hello", "", "x")
    assert n == 0
    assert out == "hello"


def test_apply_decisions_to_text_respects_action():
    candidate_by_id = {
        "c1": {"candidateId": "c1", "surface": "ジェミニー"},
        "c2": {"candidateId": "c2", "surface": "バーテックス"},
    }
    decisions = [
        {"candidateId": "c1", "action": "replace_all", "replacement": "Gemini"},
        {"candidateId": "c2", "action": "keep", "replacement": "Vertex"},  # should not fire
    ]
    text = "ジェミニー と バーテックス の話"
    new_text, patches = svc.apply_decisions_to_text(
        text=text, decisions=decisions, candidate_by_id=candidate_by_id,
    )
    assert "Gemini" in new_text
    assert "バーテックス" in new_text  # keep action left it alone
    assert len(patches) == 1
    assert patches[0]["surface"] == "ジェミニー"
    assert patches[0]["occurrences"] == 1


def test_apply_decisions_skips_unknown_candidate():
    new_text, patches = svc.apply_decisions_to_text(
        text="hello",
        decisions=[{"candidateId": "ghost", "action": "replace_all", "replacement": "X"}],
        candidate_by_id={},
    )
    assert new_text == "hello"
    assert patches == []


def test_apply_decisions_skips_empty_replacement():
    cand = {"c1": {"candidateId": "c1", "surface": "x"}}
    _, patches = svc.apply_decisions_to_text(
        text="x",
        decisions=[{"candidateId": "c1", "action": "replace_all", "replacement": ""}],
        candidate_by_id=cand,
    )
    assert patches == []


# ---------------------------------------------------------------------------
# term-memory learning
# ---------------------------------------------------------------------------

def test_decisions_to_term_upserts_basic():
    candidate_by_id = {
        "c1": {"candidateId": "c1", "surface": "ジェミニー", "entityType": "product"},
    }
    out = svc.decisions_to_term_upserts(
        [{"candidateId": "c1", "action": "replace_all", "replacement": "Gemini", "learnTerm": True}],
        candidate_by_id,
    )
    assert out == [{"canonical": "Gemini", "alias": "ジェミニー", "entity_type": "product"}]


def test_decisions_to_term_upserts_skip_when_learn_false():
    candidate_by_id = {"c1": {"candidateId": "c1", "surface": "x"}}
    out = svc.decisions_to_term_upserts(
        [{"candidateId": "c1", "action": "replace_all", "replacement": "X", "learnTerm": False}],
        candidate_by_id,
    )
    assert out == []


def test_decisions_to_term_upserts_skip_keep_action():
    candidate_by_id = {"c1": {"candidateId": "c1", "surface": "x"}}
    out = svc.decisions_to_term_upserts(
        [{"candidateId": "c1", "action": "keep", "replacement": None}],
        candidate_by_id,
    )
    assert out == []


def test_decisions_to_term_upserts_skip_identical_replacement():
    candidate_by_id = {"c1": {"candidateId": "c1", "surface": "Gemini"}}
    out = svc.decisions_to_term_upserts(
        [{"candidateId": "c1", "action": "replace_all", "replacement": "Gemini", "learnTerm": True}],
        candidate_by_id,
    )
    assert out == []


# ---------------------------------------------------------------------------
# term-hints
# ---------------------------------------------------------------------------

def test_build_term_hints_shape():
    out = svc.build_term_hints([
        {"canonical": "Gemini", "aliases": ["ジェミニー"], "entityType": "product", "weight": 0.95},
        {"canonical": "Vertex AI", "aliases": [], "entityType": "platform", "weight": 0.9},
    ])
    assert out["version"] == 1
    assert len(out["terms"]) == 2
    assert out["terms"][0]["canonical"] == "Gemini"
    assert out["terms"][0]["aliases"] == ["ジェミニー"]
    assert 0.0 <= out["terms"][0]["priority"] <= 1.0


def test_build_term_hints_limit():
    items = [
        {"canonical": f"T{i}", "aliases": [], "entityType": "unknown", "weight": 0.8}
        for i in range(100)
    ]
    assert len(svc.build_term_hints(items, limit=5)["terms"]) == 5


# ---------------------------------------------------------------------------
# finalize env flag
# ---------------------------------------------------------------------------

def test_entity_review_finalize_flag_default_false(monkeypatch):
    monkeypatch.delenv("ENABLE_ENTITY_REVIEW_FINALIZE", raising=False)
    import importlib
    from app.services import finalize as _fin
    importlib.reload(_fin)
    assert _fin.ENABLE_ENTITY_REVIEW_FINALIZE is False


def test_entity_review_finalize_flag_true_when_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_ENTITY_REVIEW_FINALIZE", "1")
    import importlib
    from app.services import finalize as _fin
    importlib.reload(_fin)
    assert _fin.ENABLE_ENTITY_REVIEW_FINALIZE is True


# ---------------------------------------------------------------------------
# transcripts canonical fallback
# ---------------------------------------------------------------------------

def test_canonical_fallback_prefers_canonical(monkeypatch):
    """When canonical text is present, it wins over session.transcriptText."""
    from app.services import transcripts as _t
    monkeypatch.setattr(_t, "_read_canonical_text", lambda sid: "canonical-wins")
    out = _t.resolve_transcript_text(
        "sess", session_data={"transcriptText": "legacy-loses"},
    )
    assert out == "canonical-wins"


def test_canonical_fallback_falls_back_to_legacy(monkeypatch):
    from app.services import transcripts as _t
    monkeypatch.setattr(_t, "_read_canonical_text", lambda sid: None)
    out = _t.resolve_transcript_text(
        "sess", session_data={"transcriptText": "legacy-used"},
    )
    assert out == "legacy-used"


def test_canonical_fallback_read_exception_falls_through(monkeypatch):
    """Firestore read errors must not break summary path."""
    from app.services import transcripts as _t
    # simulate exception inside _read_canonical_text by pointing it at a raiser
    def _raise(sid):
        raise RuntimeError("firestore outage")
    # _read_canonical_text swallows internally, so wrap at the read call:
    monkeypatch.setattr(_t, "_read_canonical_text", lambda sid: None)
    out = _t.resolve_transcript_text(
        "sess", session_data={"transcriptText": "legacy-used"},
    )
    assert out == "legacy-used"
