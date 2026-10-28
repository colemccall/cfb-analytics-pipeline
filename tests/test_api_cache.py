"""The API cache has no expiry; CFB_REFRESH_YEARS is the opt-in that bypasses it.

Needed because a season being played changes under the cache: 2026 was cached in
August and an October re-harvest returned August. No network: requests.get is
replaced and CACHE_DIR points at a temp folder.
"""
import json

import pytest

from utils import api_client


class _Resp:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


@pytest.fixture
def client(tmp_path, monkeypatch):
    """api_client with a temp cache and a counting fake network."""
    monkeypatch.setattr(api_client, "CACHE_DIR", str(tmp_path))
    calls = []

    def fake_get(url, **kw):
        calls.append(kw.get("params"))
        return _Resp([{"served": len(calls)}])

    monkeypatch.setattr(api_client.requests, "get", fake_get)
    monkeypatch.delenv("CFB_REFRESH_YEARS", raising=False)
    return calls


def test_a_cached_response_is_served_without_a_request(client):
    api_client._get("k", "/games", {"year": 2025})
    api_client._get("k", "/games", {"year": 2025})
    assert len(client) == 1


def test_a_refresh_year_skips_the_cache_and_overwrites_it(client, monkeypatch):
    api_client._get("k", "/games", {"year": 2026})
    monkeypatch.setenv("CFB_REFRESH_YEARS", "2026")
    fresh = api_client._get("k", "/games", {"year": 2026})
    assert len(client) == 2 and fresh == [{"served": 2}]
    monkeypatch.delenv("CFB_REFRESH_YEARS")
    # the refreshed answer is now what the cache holds
    assert api_client._get("k", "/games", {"year": 2026}) == [{"served": 2}]
    assert len(client) == 2


def test_other_years_still_use_the_cache_when_one_is_refreshed(client, monkeypatch):
    api_client._get("k", "/games", {"year": 2025})
    monkeypatch.setenv("CFB_REFRESH_YEARS", "2026")
    api_client._get("k", "/games", {"year": 2025})
    api_client._get("k", "/teams/fbs")          # no year parameter at all
    assert len(client) == 2                     # 2025 once, /teams/fbs once


def test_refresh_years_parses_a_list_and_ignores_junk(monkeypatch):
    monkeypatch.setenv("CFB_REFRESH_YEARS", "2026, 2025,abc,")
    assert api_client.refresh_years() == {2025, 2026}
    monkeypatch.delenv("CFB_REFRESH_YEARS")
    assert api_client.refresh_years() == set()
