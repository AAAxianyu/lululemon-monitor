#!/usr/bin/env python3
"""Read-only monitor with an IAB handoff by default. Legacy Chrome is opt-in."""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from checkout_guards import StopCheckout, select_candidate
from lululemon_monitor import check, PRODUCT_NAME, PAGE_URL, BASE

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "checkout_data"
PRIVATE = ROOT / ".private"
SHIPPING = PRIVATE / "shipping.json"
BEIJING = ZoneInfo("Asia/Shanghai")
TEST_MODE = "stop_before_place_order"


def publish(stage, **details):
    OUTPUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUTPUT.chmod(0o700)
    data = {"stage": stage, "mode": TEST_MODE,
            "updated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), **details}
    path = OUTPUT / "status.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temp.chmod(0o600)
    temp.replace(path)
    # No contact/address, payment fields, cookies, tokens, or page HTML in logs.
    print(json.dumps(data, ensure_ascii=False), flush=True)
    with (OUTPUT / "events.jsonl").open("a") as log:
        log.write(json.dumps(data, ensure_ascii=False) + "\n")
    return data


def notify(message):
    # Local Mac only. No phone delivery is claimed or configured.
    if sys.platform == "darwin":
        script = 'on run argv\ndisplay notification (item 1 of argv) with title "lululemon 测试程序" sound name "Glass"\nend run'
        subprocess.run(["osascript", "-e", script, message], capture_output=True, check=False)


@contextlib.contextmanager
def exclusive_run():
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    PRIVATE.chmod(0o700)
    with (PRIVATE / "checkout.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StopCheckout("已有程序在运行，拒绝第二个实例") from None
        lock.write(str(os.getpid()))
        lock.flush()
        yield


def sleep_until(target):
    while True:
        remaining = (target - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 30))


def select_page(context):
    pages = [p for p in context.pages if p.url.startswith("https://www.lululemon.com.au/")]
    if len(pages) > 1:
        raise StopCheckout("专用浏览器中有多个 lululemon 页面，请保留一个后重试")
    if pages:
        return pages[0]
    blank = [p for p in context.pages if p.url == "about:blank"]
    return blank[0] if len(blank) == 1 else context.new_page()


def browser_prepare():
    from playwright.sync_api import sync_playwright
    from checkout_browser import connect
    with sync_playwright() as pw:
        _, context = connect(pw)
        page = select_page(context)
        if page.url == "about:blank":
            page.goto(BASE + "/en-au/home", wait_until="domcontentloaded")
        page.bring_to_front()
        if "Access Denied" in page.locator("body").inner_text():
            raise StopCheckout("官网拒绝了商品页访问；专用 Chrome 已保留，请先从官网首页正常打开商品")
        publish("browser_prepared", message="已打开专用 Chrome；没有加购或付款。浏览器在程序退出后保持打开。")
        # Disconnect only: never browser.close() / context.close().


def prepare_checkout(candidate, resume=False, browser="iab"):
    if browser == "iab":
        OUTPUT.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = OUTPUT / "candidate.json"
        path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2))
        path.chmod(0o600)
        publish("awaiting_iab_executor", candidate=candidate, browser="iab",
                message="候选已准备；需在现有内置浏览器中运行 iab_runner.mjs，不会另开 Chrome 或提交订单。")
        notify("发现精确目标，等待内置浏览器执行器")
        return
    from playwright.sync_api import sync_playwright
    from checkout_browser import connect
    from checkout_ui import CheckoutUI
    if not SHIPPING.exists():
        raise StopCheckout("缺少本机收货配置 .private/shipping.json")
    shipping = json.loads(SHIPPING.read_text())
    fresh = select_candidate(check(us_size="XS"))
    if fresh is None or any(fresh[k] != candidate[k] for k in ("sku", "color", "au_size", "unit_price")):
        raise StopCheckout("结账前复查发现目标变化或已无货，停止")
    candidate = fresh
    with sync_playwright() as pw:
        _, context = connect(pw)
        page = select_page(context)
        page.bring_to_front()
        ui = CheckoutUI(page, publish, shipping, OUTPUT, manual_wait_seconds=1800, notify=notify)
        try:
            ui.run(candidate, resume=resume)
        except StopCheckout:
            raise
        except Exception as error:
            # Playwright errors can include filled values: do not emit their content.
            raise StopCheckout(f"浏览器步骤失败（{type(error).__name__}），请查看页面；不会重复加购或提交") from None
    notify("已停在 Place order 前，请核对商品和 Mastercard 尾号 0000")


def watch(start, end, interval, browser="iab"):
    if dt.datetime.now(dt.timezone.utc) >= end:
        raise StopCheckout("监控时间窗已经结束，不会自动延长")
    publish("scheduled", starts_at=start.isoformat(), ends_at=end.isoformat(), interval_seconds=interval)
    sleep_until(start)
    failures = 0
    while dt.datetime.now(dt.timezone.utc) < end:
        result = check(us_size="XS")
        if result.get("has_request_error") or result.get("status") == "query_failed":
            failures += 1
            publish("query_failed", message="接口失败，本次无法判断是否上架", failures=failures)
        else:
            failures = 0
            candidate = select_candidate(result)
            publish("monitoring", catalog_status=result["status"], checked_at_utc=result["checked_at_utc"])
            if candidate:
                if dt.datetime.now(dt.timezone.utc) >= end:
                    break
                publish("candidate_found", candidate=candidate)
                notify("发现 Classic Lavender Rock US XS，正在交接浏览器执行器")
                # Persist before any cart mutation. Restart requires explicit --resume.
                (OUTPUT / "candidate.json").write_text(json.dumps(candidate, ensure_ascii=False, indent=2))
                if browser == "chrome":
                    (PRIVATE / "cart-attempted").write_text(candidate["sku"])
                prepare_checkout(candidate, browser=browser)
                return
        delay = min(interval * 2 ** min(failures, 4), 900)
        for req in result.get("requests", []):
            retry = req.get("response_headers", {}).get("retry-after", "")
            if retry and str(retry).isdigit():
                delay = max(delay, int(retry))
        sleep_until(min(end, dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay)))
    publish("window_finished", message="时间窗结束，没有提交订单")


def parse_time(value):
    parsed = dt.datetime.fromisoformat(value)
    return parsed.replace(tzinfo=BEIJING) if parsed.tzinfo is None else parsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="One read-only API check; no browser actions")
    action.add_argument("--prepare-browser", action="store_true", help="Open a dedicated Chrome window only")
    action.add_argument("--watch", action="store_true", help="Bounded monitoring then checkout preparation")
    action.add_argument("--resume", action="store_true", help="Recheck and resume the recorded candidate; no re-add")
    parser.add_argument("--start", type=parse_time, default=parse_time("2026-11-17T04:00:00+08:00"))
    parser.add_argument("--end", type=parse_time, default=parse_time("2026-11-17T08:00:00+08:00"))
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--browser", choices=("iab", "chrome"), default="iab",
                        help="Default IAB handoff; chrome is the previous rehearsal only")
    args = parser.parse_args()
    if args.interval < 30 or not 0 < (args.end-args.start).total_seconds() <= 8*3600:
        parser.error("interval must be >=30; window must be >0 and <=8 hours")
    try:
        with exclusive_run():
            if args.check:
                result = check(us_size="XS")
                candidate = select_candidate(result)
                publish("checked", catalog_status=result["status"], candidate=candidate)
            elif args.prepare_browser:
                if args.browser == "iab":
                    publish("iab_session_required", message="默认沿用内置浏览器，请在 Codex 中提供现有标签页。没有启动 Chrome。")
                else:
                    browser_prepare()
            elif args.resume:
                recorded = json.loads((OUTPUT / "candidate.json").read_text())
                candidate = select_candidate(check(us_size="XS"))
                if candidate is None or any(candidate[k] != recorded[k] for k in
                                            ("sku", "unit_price", "color", "au_size")):
                    raise StopCheckout("目标商品已变化或无货，不能恢复")
                prepare_checkout(candidate, resume=True, browser=args.browser)
            else:
                if args.browser == "chrome" and (PRIVATE / "cart-attempted").exists():
                    raise StopCheckout("已有加购尝试记录，请核对浏览器后使用 --resume；不自动重复加购")
                watch(args.start, args.end, args.interval, browser=args.browser)
    except KeyboardInterrupt:
        publish("stopped", message="程序已停止；专用 Chrome 保持打开")
        return 130
    except StopCheckout as error:
        publish("needs_attention", message=str(error))
        notify(str(error))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
