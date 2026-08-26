#!/usr/bin/env python3
"""Read-only, in-memory PayAI/Agent402 catalog and contract audit."""

import concurrent.futures
import json
import sys
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse


UA = "agent-guild-pilot-readonly-audit/1.0"
PAYAI = "https://facilitator.payai.network/discovery/resources"
AGENT402_PRICING = "https://agent402.tools/api/pricing"
AGENT402_OPENAPI = "https://agent402.tools/openapi.json"
PAGE = 1000
NETWORK_SLUGS = {
    "extract", "meta", "render", "screenshot", "pdf", "http-check",
    "a2a-card-validate",
    "dns", "tls-cert", "whois", "robots-check", "sitemap",
}


def get_json(url, timeout=40):
    request = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def page(offset):
    return get_json(f"{PAYAI}?limit={PAGE}&offset={offset}", timeout=60)


def is_agent402(item):
    try:
        return (urlparse(item.get("resource", "")).hostname or "").lower() == "agent402.tools"
    except Exception:
        return False


def concise_record(item):
    return {
        "resource": item.get("resource"),
        "method": item.get("method"),
        "lastUpdated": item.get("lastUpdated"),
        "description": item.get("description") or (item.get("metadata") or {}).get("description"),
        "mimeType": item.get("mimeType") or (item.get("metadata") or {}).get("mimeType"),
        "inputSchema": item.get("inputSchema"),
        "outputSchema": item.get("outputSchema"),
        "accepts": item.get("accepts"),
    }


def main():
    first = page(0)
    total = int((first.get("pagination") or {}).get("total", len(first.get("items", []))))
    offsets = list(range(PAGE, total, PAGE))
    pages = {0: first}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(page, offset): offset for offset in offsets}
        for future in concurrent.futures.as_completed(futures):
            pages[futures[future]] = future.result()
    items = []
    for offset in sorted(pages):
        items.extend(pages[offset].get("items", []))
    matches = [item for item in items if is_agent402(item)]
    by_resource = {item.get("resource"): item for item in matches}

    pricing = get_json(AGENT402_PRICING)
    endpoints = pricing.get("endpoints", [])
    network_tools = [item for item in endpoints if item.get("slug") in NETWORK_SLUGS]
    extract_price = next((item for item in endpoints if item.get("slug") == "extract"), None)

    openapi = get_json(AGENT402_OPENAPI)
    extract_op = openapi.get("paths", {}).get("/api/extract", {}).get("post", {})
    extract_contract = {
        "method": "POST",
        "endpoint": "https://agent402.tools/api/extract",
        "summary": extract_op.get("summary"),
        "description": extract_op.get("description"),
        "requestBody": extract_op.get("requestBody"),
        "parameters": extract_op.get("parameters"),
        "responses": extract_op.get("responses"),
        "pricing_record": extract_price,
    }
    validator_op = openapi.get("paths", {}).get("/api/a2a-card-validate", {}).get("post", {})
    validator_price = next((item for item in endpoints if item.get("slug") == "a2a-card-validate"), None)
    validator_contract = {
        "method": "POST",
        "endpoint": "https://agent402.tools/api/a2a-card-validate",
        "summary": validator_op.get("summary"),
        "description": validator_op.get("description"),
        "requestBody": validator_op.get("requestBody"),
        "parameters": validator_op.get("parameters"),
        "responses": validator_op.get("responses"),
        "pricing_record": validator_price,
    }

    focus_urls = [
        "https://agent402.tools/api/a2a-card-validate",
        "https://agent402.tools/api/extract",
        "https://agent402.tools/api/http-check",
        "https://agent402.tools/api/meta",
        "https://agent402.tools/api/render",
        "https://agent402.tools/api/hash",
    ]
    result = {
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "read_only_in_memory_no_tool_invocation",
        "payai": {
            "catalog_url": PAYAI,
            "declared_total": total,
            "records_read": len(items),
            "agent402_record_count": len(matches),
            "agent402_unique_resource_count": len(by_resource),
            "sample_resources": sorted(by_resource)[:12],
            "a2a_related_records": [
                concise_record(item) for item in matches
                if "a2a" in json.dumps(item, ensure_ascii=False).lower()
            ],
            "focus_records": [concise_record(by_resource[url]) for url in focus_urls if url in by_resource],
        },
        "agent402": {
            "pricing_url": AGENT402_PRICING,
            "openapi_url": AGENT402_OPENAPI,
            "extract_contract": extract_contract,
            "a2a_card_validate_contract": validator_contract,
            "network_tools": network_tools,
            "all_network_tools_compute_payable_false": bool(network_tools) and all(
                item.get("computePayable") is False for item in network_tools
            ),
        },
    }
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
