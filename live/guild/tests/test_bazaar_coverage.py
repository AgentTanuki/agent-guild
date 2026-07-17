"""Eventually-complete Bazaar discovery (machine-attribution pass).

Primary-doc research (docs.cdp.coinbase.com/x402/bazaar, 2026-07-15):
  * `GET /v2/x402/discovery/search` — documented server-side semantic
    search (query/filters, limit ≤ 20, response {"resources": [...]}) —
    USED FIRST for capability matching;
  * `GET /v2/x402/discovery/resources` — paginated catalog (limit default
    100, max 1000, offset), 25k+ items, "browse order (newest first)".

Defect reproduced: the catalogue fallback restarted at offset 0 every run,
scanning the same first 300 of 25k+ items forever — most of the catalogue
was structurally unreachable. Fix: a persisted per-capability cursor sweeps
a bounded number of pages per cycle until the catalogue has been completely
swept, then restarts a fresh sweep; coverage/cursor/sweep stats recorded;
duplicates and catalogue shrinkage handled; restarts resume the cursor.
"""
import uuid

import pytest

from app.state import store
from app.swarm import scout


def _cap():
    return "bz-cap-" + uuid.uuid4().hex[:8]


@pytest.fixture(autouse=True)
def _clean():
    store.swarm_state.pop(scout.SCOUT_STATE_KEY, None)
    yield
    store.swarm_state.pop(scout.SCOUT_STATE_KEY, None)


class FakeBazaar:
    """A deterministic catalogue with search + paginated resources."""

    def __init__(self, total=500, search_hits=None):
        self.total = total
        self.search_hits = search_hits or []
        self.search_calls = 0
        self.page_offsets: list[int] = []

    def fetch(self, url, **kw):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(url).query)
        if "/discovery/search" in url:
            self.search_calls += 1
            return ({"resources": list(self.search_hits),
                     "partialResults": False, "searchMethod": "vector",
                     "x402Version": 2}, "ok")
        if "/discovery/resources" in url:
            limit = int(q.get("limit", ["100"])[0])
            offset = int(q.get("offset", ["0"])[0])
            self.page_offsets.append(offset)
            items = [{"resource": f"https://s{i}.example/paid",
                      "type": "http", "x402Version": 1,
                      "accepts": [{"scheme": "exact",
                                   "network": "eip155:8453",
                                   "asset": "0x" + "aa" * 20,
                                   "amount": "1", "payTo": "0x" + "bb" * 20}],
                      "description": f"item {i}"}
                     for i in range(offset, min(offset + limit, self.total))]
            return ({"items": items,
                     "pagination": {"limit": limit, "offset": offset,
                                    "total": self.total}}, "ok")
        return (None, "http_404")


def test_documented_server_side_search_is_used_first():
    cap = _cap()
    hit = {"resource": "https://hit.example/paid", "type": "http",
           "x402Version": 2, "description": f"does {cap}",
           "accepts": [{"scheme": "exact", "network": "eip155:8453",
                        "asset": "0x" + "aa" * 20, "amount": "1",
                        "payTo": "0x" + "cc" * 20}]}
    fb = FakeBazaar(search_hits=[hit])
    out = scout.adapter_x402_bazaar(cap, fb.fetch, store=store)
    assert fb.search_calls >= 1, (
        "the documented /discovery/search endpoint must be used for "
        "capability matching")
    assert any(c["endpoint"] == "https://hit.example/paid" for c in out)


def test_catalogue_sweep_resumes_from_persisted_cursor():
    cap = _cap()
    fb = FakeBazaar(total=500, search_hits=[])
    scout.adapter_x402_bazaar(cap, fb.fetch, store=store)
    first_offsets = list(fb.page_offsets)
    assert first_offsets and first_offsets[0] == 0
    fb.page_offsets.clear()
    scout.adapter_x402_bazaar(cap, fb.fetch, store=store)
    second_offsets = list(fb.page_offsets)
    assert second_offsets, "the sweep must continue on the next cycle"
    assert second_offsets[0] > first_offsets[-1], (
        "the sweep restarted at offset 0 instead of resuming from the "
        f"persisted cursor (first={first_offsets}, second={second_offsets})")


def test_sweep_completes_and_records_coverage():
    cap = _cap()
    fb = FakeBazaar(total=450, search_hits=[])
    for _ in range(10):
        scout.adapter_x402_bazaar(cap, fb.fetch, store=store)
        stats = scout.bazaar_sweep_stats(store, cap)
        if stats.get("last_complete_sweep_at"):
            break
    stats = scout.bazaar_sweep_stats(store, cap)
    assert stats["last_complete_sweep_at"], (
        "bounded pages per cycle must EVENTUALLY sweep the whole catalogue")
    assert stats["pages_scanned"] >= 1
    assert stats["catalogue_total"] == 450
    assert 0 <= stats["coverage"] <= 1.0
    assert "cursor" in stats


def test_cursor_beyond_shrunk_catalogue_restarts_cleanly():
    cap = _cap()
    st = store.swarm_state.setdefault(scout.SCOUT_STATE_KEY, {})
    st.setdefault("bazaar_sweeps", {})[scout.canonical_sweep_key(cap)] = {
        "cursor": 10_000, "pages_scanned": 3, "candidates_found": 0}
    fb = FakeBazaar(total=120, search_hits=[])
    scout.adapter_x402_bazaar(cap, fb.fetch, store=store)
    stats = scout.bazaar_sweep_stats(store, cap)
    assert stats["cursor"] <= 120, (
        "an expired/out-of-range cursor must reset, not loop past the end")


def test_duplicate_items_across_pages_yield_one_candidate():
    cap = _cap()

    def fetch(url, **kw):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(url).query)
        if "/discovery/search" in url:
            return ({"resources": [], "partialResults": False}, "ok")
        offset = int(q.get("offset", ["0"])[0])
        if offset > 200:
            return ({"items": [], "pagination": {"total": 200}}, "ok")
        item = {"resource": "https://dup.example/paid", "type": "http",
                "x402Version": 1, "description": f"does {cap}",
                "accepts": [{"scheme": "exact", "network": "eip155:8453",
                             "asset": "0x" + "aa" * 20, "amount": "1",
                             "payTo": "0x" + "dd" * 20}]}
        return ({"items": [item] * 100,
                 "pagination": {"limit": 100, "offset": offset,
                                "total": 200}}, "ok")

    out = scout.adapter_x402_bazaar(cap, fetch, store=store)
    endpoints = [c["endpoint"] for c in out]
    assert endpoints.count("https://dup.example/paid") == 1
