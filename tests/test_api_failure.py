"""A failed API call must not look like "no data" (v4.24).

Until v4.24 `_get_list` returned [] for every failure, so a 500 or a dropped connection read as an empty season and a
harvest that replaces a season wrote nothing over the stored rows. These tests pin the new behaviour: only a 404 is
"nothing there"; everything else raises; and the cache write cannot leave a truncated file.
"""

import json
import os

import pytest
import requests

from utils import api_client


class _Resp:
    """Just enough of requests.Response for api_client._request."""

    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """The API client pointed at an empty cache folder, with no refresh switch set and no sleeping."""
    monkeypatch.setattr(api_client, "CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("CFB_REFRESH_YEARS", raising=False)
    monkeypatch.setattr(api_client.time, "sleep", lambda s: None)
    return tmp_path


def _serve(monkeypatch, *responses):
    """Make requests.get return the given responses in order, and count the calls."""
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return responses[min(len(calls) - 1, len(responses) - 1)]
    monkeypatch.setattr(api_client.requests, "get", fake_get)
    return calls


def test_a_server_error_raises_instead_of_returning_nothing(cache, monkeypatch):
    """A 500 is a failure, not an empty season."""
    _serve(monkeypatch, _Resp(500))
    with pytest.raises(requests.HTTPError):
        api_client._get_list("key", "/games", {"year": 2026})


def test_a_dropped_connection_raises(cache, monkeypatch):
    """A network error is a failure too."""
    def boom(url, **kw):
        raise requests.ConnectionError("no route")
    monkeypatch.setattr(api_client.requests, "get", boom)
    with pytest.raises(requests.ConnectionError):
        api_client._get_list("key", "/games", {"year": 2026})


def test_exhausted_rate_limit_retries_raise(cache, monkeypatch):
    """Five 429s in a row is a failure, not an empty list."""
    _serve(monkeypatch, _Resp(429))
    with pytest.raises(RuntimeError, match="rate-limited"):
        api_client._get_list("key", "/games", {"year": 2026})


def test_a_404_is_no_data(cache, monkeypatch):
    """The API answering 'not found' is the one failure that means there is nothing there."""
    _serve(monkeypatch, _Resp(404))
    assert api_client._get_list("key", "/awards", {"year": 2026}) == []


def test_a_failure_leaves_nothing_in_the_cache(cache, monkeypatch):
    """A failed request must not be cached as an empty answer."""
    _serve(monkeypatch, _Resp(500))
    with pytest.raises(requests.HTTPError):
        api_client._get_list("key", "/games", {"year": 2026})
    assert os.listdir(cache) == []


def test_a_good_response_is_cached_and_served_again(cache, monkeypatch):
    """The ordinary path is unchanged: one request, then the cache."""
    calls = _serve(monkeypatch, _Resp(200, [{"id": 1}]))
    assert api_client._get_list("key", "/games", {"year": 2026}) == [{"id": 1}]
    assert api_client._get_list("key", "/games", {"year": 2026}) == [{"id": 1}]
    assert len(calls) == 1


def test_a_killed_write_leaves_the_old_response(cache, monkeypatch):
    """The cache is written to a temp file and renamed: dying mid-write cannot truncate the stored response."""
    api_client.cache_write("/games", {"year": 2026}, [{"id": 1}])

    def die(obj, fh, *a, **kw):
        fh.write("[{")
        raise KeyboardInterrupt
    monkeypatch.setattr(api_client.json, "dump", die)
    with pytest.raises(KeyboardInterrupt):
        api_client.cache_write("/games", {"year": 2026}, [{"id": 2}])
    monkeypatch.undo()
    monkeypatch.setattr(api_client, "CACHE_DIR", str(cache))
    assert api_client.cache_read("/games", {"year": 2026}) == [{"id": 1}]


def test_an_unreadable_cache_file_is_fetched_again(cache, monkeypatch):
    """A truncated file from an older, non-atomic run counts as a miss, not a permanent failure."""
    with open(api_client.cache_path("/games", {"year": 2026}), "w") as f:
        f.write('[{"id": 1')
    calls = _serve(monkeypatch, _Resp(200, [{"id": 9}]))
    assert api_client._get_list("key", "/games", {"year": 2026}) == [{"id": 9}]
    assert len(calls) == 1
    assert json.load(open(api_client.cache_path("/games", {"year": 2026}))) == [{"id": 9}]


def test_script_09_exits_non_zero_and_names_a_failed_dataset(monkeypatch, capsys):
    """One dead endpoint must not stop the others, but the run must not report success."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("s09", Path(api_client.__file__).parent.parent / "scripts" / "09_harvest_supplemental.py")
    s09 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s09)

    def fake_harvest(name, seasons, api_key, dry_run=False):
        if name == "coaches":
            raise requests.HTTPError("HTTP 500")
        return 5
    monkeypatch.setattr(s09, "harvest_one", fake_harvest)
    monkeypatch.setattr(s09, "load_api_key", lambda: "key")
    monkeypatch.setattr("sys.argv", ["09", "--dataset", "coaches", "draft_picks", "--seasons", "2026"])
    with pytest.raises(SystemExit) as exit_info:
        s09.main()
    assert "coaches" in str(exit_info.value) and "draft_picks" not in str(exit_info.value)
    assert "5 rows written" in capsys.readouterr().out      # the healthy dataset still ran
