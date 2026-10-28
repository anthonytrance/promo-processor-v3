"""Shared test setup: the synthetic test audio is six seconds long, far under the real preview-clip floor."""
import pytest

import promo_pipeline
import promo_automation


@pytest.fixture(autouse=True)
def short_test_audio(monkeypatch):
    monkeypatch.setattr(promo_pipeline, "PREVIEW_SECONDS", 5)


@pytest.fixture(autouse=True)
def offline_exclusions(monkeypatch, tmp_path):
    """Tests never read the user's live exclusion CSV from Dropbox or its local copy."""
    monkeypatch.setattr(promo_pipeline, "EXCLUSIONS_REMOTE", None)
    monkeypatch.setattr(promo_pipeline, "EXCLUSIONS_CACHE", tmp_path / "no-exclusions.csv")
    monkeypatch.delitem(promo_automation.SETTINGS, "additional_library_roots", raising=False)
    monkeypatch.delitem(promo_automation.SETTINGS, "exclusion_samples_folder", raising=False)
