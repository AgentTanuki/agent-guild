"""Heavy aggregate reports are served from a TTL cache (2026-10-13): one
computation per TTL, stampede-safe, freshness stated on the response, and
disabled when the TTL is 0 (the test default)."""
import os
import threading
import time

os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app import reportcache  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def test_ttl_zero_recomputes_every_time(monkeypatch):
    monkeypatch.setenv("GUILD_REPORT_CACHE_TTL_S", "0")
    calls = []
    for _ in range(3):
        reportcache.cached("t0", lambda: calls.append(1) or {"n": len(calls)})
    assert len(calls) == 3


def test_cached_within_ttl_and_annotated(monkeypatch):
    monkeypatch.setenv("GUILD_REPORT_CACHE_TTL_S", "60")
    reportcache.clear()
    calls = []
    a = reportcache.cached("t1", lambda: calls.append(1) or {"n": len(calls)})
    b = reportcache.cached("t1", lambda: calls.append(1) or {"n": len(calls)})
    assert len(calls) == 1 and a["n"] == b["n"] == 1
    assert b["_report_cache"]["age_s"] >= 0 and b["_report_cache"]["ttl_s"] == 60
    assert "computed_at" in b["_report_cache"]


def test_stampede_protection_computes_once(monkeypatch):
    monkeypatch.setenv("GUILD_REPORT_CACHE_TTL_S", "60")
    reportcache.clear()
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.3)
        return {"v": 1}

    threads = [threading.Thread(target=lambda: reportcache.cached("t2", slow))
               for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1


def test_heavy_endpoints_carry_freshness_when_cached(monkeypatch):
    monkeypatch.setenv("GUILD_REPORT_CACHE_TTL_S", "60")
    reportcache.clear()
    for path in ("/commercial", "/funnel/paid", "/funnel", "/funnel/passports"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "_report_cache" in r.json(), path
    # unknown operation is still refused, never served from a stale cache
    assert client.get("/commercial?operation=nope").status_code == 400
    reportcache.clear()
