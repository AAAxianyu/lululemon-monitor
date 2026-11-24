#!/usr/bin/env python3
"""Read-only AU Classic/Lavender Rock monitor. Default: one fresh check.

Requires curl_cffi. No cart, account, payment, or order requests are made.
"""
import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import os
import tempfile
from urllib.parse import urlencode

PRODUCT_ID = "lopmi7nqah"
PRODUCT_NAME = "Scuba Evolve Classic Full-Zip Hoodie"
BASE = "https://www.lululemon.com.au"
PRODUCT_URL = f"{BASE}/.uf-svc/v1/products/{PRODUCT_ID}"
STOCK_URL = PRODUCT_URL + "/skus/availability?fulfillment=ship"
PAGE_URL = f"{BASE}/en-au/p/scuba-evolve-classic-full-zip-hoodie/{PRODUCT_ID}"
HEADERS = {"x-lll-locale": "en-au", "Accept": "application/vnd.api+json",
           "Cache-Control": "no-cache"}
ROOT = Path(__file__).resolve().parent / "monitor_data"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def target_color(color):
    """Allow explicitly named zipper combinations, not other lavender shades."""
    name = str(color.get("name", "")).strip().casefold()
    group = re.split(r"[/\s,]+", str(color.get("colorGroup", "")).upper())
    return (str(color.get("id", "")).lstrip("0") == "77631"
            or name == "lavender rock" or name.startswith("lavender rock/")
            or name.startswith("lavender rock /") or "LAVR" in group)


def lavender_family(color):
    """Lavender Rock or a Lavender Rock/<zip> combination; name AND colour group must agree."""
    name = str(color.get("name", "")).strip().casefold()
    group = str(color.get("colorGroup", "")).strip().upper()
    return (re.fullmatch(r"lavender rock(\s*/\s*[^/]+)?", name) is not None
            and group.split("/")[0].strip() == "LAVR"
            and re.fullmatch(r"\d+", str(color.get("id", ""))) is not None)


def analyze(product, stock, us_size="XS"):
    data = product.get("data", {})
    attrs = data.get("attributes", {})
    if data.get("id") != PRODUCT_ID or attrs.get("name") != PRODUCT_NAME:
        raise ValueError("Product identity mismatch; refusing to match other Scuba cuts")
    if not all(isinstance(attrs.get(k), list) for k in ("colors", "skus", "sizes")):
        raise ValueError("Product schema changed: colors/skus/sizes missing")
    size_keys = {str(s["key"]) for s in attrs["sizes"] if any(
        x.get("region") == "US" and x.get("label", "").upper() == us_size.upper()
        for x in s.get("sizeSystems", []))}
    colors = {str(c["id"]): c for c in attrs["colors"] if target_color(c)}
    target_skus = [s for s in attrs["skus"] if target_color(s.get("color", {}))]
    for s in target_skus:
        colors[str(s["color"]["id"])] = s["color"]
    stock_map = {}
    if stock is not None:
        records = stock.get("data")
        if not isinstance(records, list) or not all(
            isinstance(s, dict) and s.get("type") == "skus.availability"
            and isinstance(s.get("attributes"), dict) for s in records):
            raise ValueError("Inventory schema changed")
        stock_map = {str(s["id"]): s["attributes"] for s in records}
    rows = []
    for s in target_skus:
        a = stock_map.get(str(s["id"]), {})
        rows.append({"sku": s["id"], "style_id": s.get("styleId"),
                     "is_final_sale": s.get("isFinalSale"),
                     "au_size": s.get("size"), "size_token": s.get("sizeTokenKey"),
                     "matches_requested_us_size": str(s.get("sizeTokenKey")) in size_keys,
                     "color": s["color"], "price": s.get("price"),
                     "is_available": a.get("isAvailable"), "is_low_stock": a.get("isLowStock"),
                     "reported_quantity": a.get("quantity"),
                     "stock_source": "availability_endpoint" if a else "unknown",
                     "catalog_link": PAGE_URL + "?" + urlencode(
                         {"color": s["color"]["id"], "skuId": s["id"]})})
    selected = [r for r in rows if r["matches_requested_us_size"]]
    if not colors:
        status = "not_in_catalog"
    elif not rows:
        status = "color_listed_no_skus"
    elif not size_keys:
        status = "size_mapping_unknown"
    elif not selected:
        status = "requested_size_not_listed"
    elif any(r["is_available"] is True for r in selected):
        status = "requested_size_available"
    elif any(r["is_available"] is None for r in selected):
        status = "stock_unknown"
    else:
        status = "requested_size_unavailable"
    return {"status": status, "product_name": attrs["name"], "requested_us_size": us_size, "requested_size_keys": sorted(size_keys),
            "target_colors": list(colors.values()), "target_skus": rows,
            "catalog_color_count": len(attrs["colors"]), "catalog_sku_count": len(attrs["skus"]),
            "badge_only_hint": any(str(c).lstrip("0") == "77631" for c in attrs.get("newColorsBadging", [])),
            "quantity_visibility": "Optional per SKU; absent quantity means unknown, not zero.",
            "note": "Availability is an API snapshot, not a stock reservation or successful checkout."}


def fetch(label, url, output):
    from curl_cffi import requests
    record = {"label": label, "method": "GET", "url": url, "requested_at_utc": now(),
              "request_headers": HEADERS}
    payload = None
    try:
        response = requests.get(url, headers=HEADERS, impersonate="chrome", timeout=25)
        body = response.content
        digest = hashlib.sha256(body).hexdigest()
        response_dir = output / "responses"
        response_dir.mkdir(parents=True, exist_ok=True)
        body_path = response_dir / f"{label}-{digest}.body"
        if not body_path.exists():
            # Concurrent readers must never see a partially written response body.
            with tempfile.NamedTemporaryFile(dir=response_dir, delete=False) as tmp:
                tmp.write(body)
                temporary_body = tmp.name
            os.replace(temporary_body, body_path)
        record.update({"status_code": response.status_code, "final_url": response.url,
                       "body_sha256": digest, "body_file": str(body_path),
                       "response_headers": {k: response.headers.get(k) for k in
                                            ("date", "age", "cache-control", "content-type", "retry-after")}})
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}: {response.text[:160]}")
        payload = response.json()
        if not isinstance(payload, dict) or "data" not in payload or payload.get("errors"):
            payload = None
            raise ValueError("API error or unrecognized JSON envelope")
    except Exception as error:
        record["error"] = str(error)
    record["completed_at_utc"] = now()
    return payload, record


def check(output=ROOT, us_size="XS", *, persist=True):
    output.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(fetch, label, url, output) for label, url in
                (("product", PRODUCT_URL), ("inventory", STOCK_URL))]
        (product, product_request), (stock, stock_request) = [j.result() for j in jobs]
    result = {"checked_at_utc": now(), "product_id": PRODUCT_ID,
              "requests": [product_request, stock_request]}
    if product is None:
        result.update(status="query_failed", error="Product request failed; absence is unknown")
    else:
        try:
            result.update(analyze(product, stock, us_size))
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            result.update(status="query_failed", error=str(error))
    result["has_request_error"] = any(r.get("error") for r in result["requests"])
    if persist:
        persist_result(result, output)
    return result


def persist_result(result, output=ROOT, *, update_latest=True):
    """Called by one coordinator when requests overlap; append all evidence in completion order."""
    output.mkdir(parents=True, exist_ok=True)
    last = output / "latest.json"
    temporary = output / "latest.tmp"
    if update_latest:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        temporary.replace(last)
    if update_latest and result["status"] != "query_failed" and not result["has_request_error"]:
        (output / "last_success.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    with (output / "checks.jsonl").open("a") as stream:
        stream.write(json.dumps(result, ensure_ascii=False) + "\n")


def notify(result):
    if sys.platform != "darwin":
        return
    title = "lululemon AU: Classic Lavender Rock"
    message = f'US {result["requested_us_size"]}: {result["status"]}'
    script = 'on run argv\ndisplay notification (item 1 of argv) with title (item 2 of argv) sound name "Glass"\nend run'
    subprocess.run(["osascript", "-e", script, message, title], check=False, capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--us-size", default="XS")
    parser.add_argument("--watch-minutes", type=float, default=0,
                        help="0=once; bounded polling for an active monitoring session")
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--notify", action="store_true", help="macOS local notification on target changes")
    parser.add_argument("--output", type=Path, default=ROOT)
    args = parser.parse_args()
    if args.interval < 30 or not 0 <= args.watch_minutes <= 480:
        parser.error("Use interval >=30 seconds and watch-minutes between 0 and 480")
    deadline = time.monotonic() + args.watch_minutes * 60
    previous = None
    existing = args.output / "latest.json"
    if existing.exists():
        try:
            old = json.loads(existing.read_text())
            previous = (old.get("status"), json.dumps(old.get("target_skus"), sort_keys=True))
        except (OSError, ValueError):
            pass
    failures = 0
    while True:
        result = check(args.output, args.us_size)
        signature = (result["status"], json.dumps(result.get("target_skus"), sort_keys=True))
        changed = signature != previous
        result["changed"] = changed
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if changed and args.notify and result["status"] not in ("not_in_catalog", "query_failed"):
            notify(result)
        previous = signature
        if args.watch_minutes == 0:
            return 2 if result["has_request_error"] or result["status"] == "query_failed" else 0
        failures = failures + 1 if result["has_request_error"] else 0
        delay = min(args.interval * 2 ** min(failures, 4), 900)
        for request in result["requests"]:
            retry_after = request.get("response_headers", {}).get("retry-after")
            if retry_after and retry_after.isdigit():
                delay = max(delay, int(retry_after))
        remaining = deadline - time.monotonic()
        if remaining <= 0 or delay >= remaining:
            return 0
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
