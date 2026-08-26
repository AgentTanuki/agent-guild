#!/usr/bin/env python3
"""Read PayAI's public catalog in memory and emit zero-price AG-action candidates."""

import concurrent.futures
import json
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse


CATALOG = "https://facilitator.payai.network/discovery/resources"
PAGE_SIZE = 1000
UA = "agent-guild-pilot-readonly-audit/1.0"
SEMANTIC_TERMS = {
    "website",
    "fetch",
    "scrape",
    "extract",
    "trust",
    "verify",
    "verification",
    "credential",
    "passport",
    "agent card",
    "a2a",
    "preflight",
}
INPUT_KEYS = {"url", "uri", "domain", "credential", "passport", "target", "endpoint"}


def get_page(offset):
    url = f"{CATALOG}?limit={PAGE_SIZE}&offset={offset}"
    request = urllib.request.Request(
        url,
        headers={"accept": "application/json", "user-agent": UA},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return offset, json.loads(response.read().decode("utf-8"))


def accepted_amount(accept):
    if accept.get("amount") is not None:
        return str(accept.get("amount"))
    if accept.get("maxAmountRequired") is not None:
        return str(accept.get("maxAmountRequired"))
    return None


def is_zero_price(item):
    accepts = item.get("accepts") or []
    return bool(accepts) and all(accepted_amount(a) == "0" for a in accepts)


def compact(item):
    return {
        "resource": item.get("resource"),
        "host": urlparse(item.get("resource") or "").hostname,
        "method": item.get("method"),
        "lastUpdated": item.get("lastUpdated"),
        "description": item.get("description")
        or (item.get("metadata") or {}).get("description"),
        "inputSchema": item.get("inputSchema"),
        "outputSchema": item.get("outputSchema"),
        "accepts": item.get("accepts"),
    }


def input_key_hits(value, parent=None):
    hits = set()
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in INPUT_KEYS:
                hits.add(lowered)
            hits.update(input_key_hits(child, lowered))
    elif isinstance(value, list):
        for child in value:
            hits.update(input_key_hits(child, parent))
    return hits


def main():
    first_offset, first = get_page(0)
    total = int((first.get("pagination") or {}).get("total", 0))
    pages = {first_offset: first}
    offsets = list(range(PAGE_SIZE, total, PAGE_SIZE))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(get_page, offset) for offset in offsets]
        for future in concurrent.futures.as_completed(futures):
            offset, page = future.result()
            pages[offset] = page

    items = []
    for offset in sorted(pages):
        items.extend(pages[offset].get("items") or [])

    zero = [item for item in items if is_zero_price(item)]
    relevant = []
    for item in zero:
        semantic_text = " ".join(
            str(value or "")
            for value in (
                item.get("description"),
                (item.get("metadata") or {}).get("description"),
                item.get("serviceName"),
                item.get("toolName"),
            )
        ).lower()
        semantic_hits = sorted(term for term in SEMANTIC_TERMS if term in semantic_text)
        key_hits = sorted(input_key_hits(item.get("inputSchema") or {}))
        if semantic_hits or key_hits:
            record = compact(item)
            record["matched_semantic_terms"] = semantic_hits
            record["matched_input_keys"] = key_hits
            relevant.append(record)

    result = {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source": CATALOG,
        "declared_total": total,
        "records_read": len(items),
        "zero_price_records": len(zero),
        "zero_price_unique_hosts": len(
            {urlparse(i.get("resource") or "").hostname for i in zero}
        ),
        "zero_price_host_counts": Counter(
            urlparse(i.get("resource") or "").hostname for i in zero
        ).most_common(),
        "relevant_zero_price_records": relevant,
        "relevant_zero_price_record_count": len(relevant),
        "mode": "read_only_in_memory_no_candidate_invocation",
    }
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
