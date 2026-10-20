"""Offline tests for the bounded BALLDONTLIE NBA history downloader."""
import io
import json
import sys
from urllib.error import HTTPError

import pytest
from scripts import nba_nhl_fetch_nba_history as dl


def sample(id, season=2025):
    return {"id": id, "season": season, "postseason": False, "status_state": "final"}


def payload(rows, cursor):
    return {"data": rows, "meta": {"next_cursor": cursor, "per_page": 100}}


def test_rejects_malformed_data_and_missing_pagination():
    # A short terminal page can omit next_cursor entirely.
    rows, cursor = dl.validate_page({"data": [sample(1)], "meta": {"per_page": 100}}, 2025)
    assert [g["id"] for g in rows] == [1]
    assert cursor is None
    # A full page without a cursor is ambiguous and must fail closed.
    with pytest.raises(ValueError, match="Full page missing pagination"):
        dl.validate_page({"data": [sample(i) for i in range(100)], "meta": {}}, 2025)
    with pytest.raises(ValueError, match="Season mismatch"):
        dl.validate_page(payload([sample(1, season=2024)], None), 2025)
    with pytest.raises(ValueError, match="Postseason"):
        dl.validate_page(payload([{**sample(1), "postseason": True}], None), 2025)


def test_resume_uses_cache_without_new_provider_calls(tmp_path, monkeypatch):
    responses = [
        payload([sample(1), sample(2)], 789),
        {"data": [sample(3)], "meta": {"per_page": 100}},
    ]
    requested = []

    def fake_open(request, timeout):
        requested.append(request.full_url)
        assert request.get_header("Authorization") == "test-secret"
        return io.BytesIO(json.dumps(responses.pop(0)).encode())

    monkeypatch.setattr(dl, "urlopen", fake_open)
    monkeypatch.setattr(dl, "getpass", lambda prompt: "test-secret")
    monkeypatch.setattr(dl.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(sys, "argv", ["history", "--season", "2025",
                                      "--max-pages", "2", "--output-dir", str(tmp_path)])
    assert dl.main() == 0
    assert len(requested) == 2
    assert "cursor=789" in requested[1]
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert m["complete_pagination"] is True
    assert m["unique_games"] == 3

    monkeypatch.setattr(dl, "urlopen", lambda *args, **kwargs: pytest.fail("Unexpected API call"))
    assert dl.main() == 0
    assert list(tmp_path.glob("page-*.json")) and len(list(tmp_path.glob("page-*.json"))) == 2


def test_stops_at_page_ceiling_without_claiming_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "urlopen", lambda *a, **k: io.BytesIO(
        json.dumps(payload([sample(1)], 999)).encode()))
    monkeypatch.setattr(dl, "getpass", lambda prompt: "test-secret")
    monkeypatch.setattr(sys, "argv", ["history", "--season", "2025",
                                      "--max-pages", "1", "--output-dir", str(tmp_path)])
    assert dl.main() == 5
    assert json.loads((tmp_path / "manifest.json").read_text())["complete_pagination"] is False


def test_http_429_never_retries(tmp_path, monkeypatch):
    hits = []

    def blocked(request, timeout):
        hits.append(request.full_url)
        raise HTTPError(request.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(dl, "urlopen", blocked)
    monkeypatch.setattr(dl, "getpass", lambda prompt: "test-secret")
    monkeypatch.setattr(sys, "argv", ["history", "--season", "2025",
                                      "--output-dir", str(tmp_path)])
    assert dl.main() == 2
    assert len(hits) == 1
    assert not list(tmp_path.glob("page-*.json"))
