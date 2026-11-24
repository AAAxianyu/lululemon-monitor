"""Fail-closed guards for the rehearsal runner. No network or payment actions."""
import datetime as dt
from decimal import Decimal, InvalidOperation
import re

from lululemon_monitor import PRODUCT_ID, PRODUCT_NAME, lavender_family

CARD_LAST4 = "0000"


class StopCheckout(RuntimeError):
    pass


def select_candidate(result, *, current_time=None):
    """Discovery may be broad; automatic checkout is deliberately exact."""
    if result.get("has_request_error") or result.get("status") == "query_failed":
        raise StopCheckout("商品或库存请求失败，不能视为有货")
    if result.get("product_id") != PRODUCT_ID or result.get("product_name") != PRODUCT_NAME:
        raise StopCheckout("商品身份不符，停止")
    current_time = current_time or dt.datetime.now(dt.timezone.utc)
    try:
        age = (current_time - dt.datetime.fromisoformat(result["checked_at_utc"])).total_seconds()
    except (ValueError, KeyError, TypeError):
        raise StopCheckout("缺少有效查询时间") from None
    if not 0 <= age <= 120:
        raise StopCheckout("库存快照已过期")
    if result.get("requested_us_size") != "XS":
        raise StopCheckout("监控尺码不是 US XS")
    if result.get("status") != "requested_size_available":
        return None
    choices = []
    for row in result.get("target_skus", []):
        if not row.get("matches_requested_us_size") or row.get("is_available") is not True:
            continue
        color = row.get("color", {})
        # A changed/combined colour must be reviewed; it must never silently replace LAVR.
        if not lavender_family(color):
            raise StopCheckout("颜色不是 Lavender Rock（或其拉链组合色），或名称与 LAVR 色组不一致")
        if row.get("stock_source") != "availability_endpoint":
            raise StopCheckout("缺少独立库存接口证据")
        if str(row.get("size_token")) not in result.get("requested_size_keys", []):
            raise StopCheckout("US XS 尺码映射不一致")
        if row.get("au_size") != "6-8":
            raise StopCheckout("AU 尺码映射发生变化")
        if row.get("is_final_sale") is not False:
            raise StopCheckout("商品是否 Final Sale 不明确，停止")
        price = row.get("price") or {}
        if price.get("currencyCode") != "AUD":
            raise StopCheckout("商品不是 AUD 计价")
        try:
            amount = Decimal(str(price.get("salePrice", price["listPrice"])))
            if not amount.is_finite() or amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError, KeyError, TypeError):
            raise StopCheckout("商品价格无法验证") from None
        if not re.fullmatch(r"\d+", str(row.get("sku", ""))):
            raise StopCheckout("SKU 格式不符合官网数据")
        choices.append({**row, "product_id": PRODUCT_ID, "product_name": PRODUCT_NAME,
                        "us_size": "XS", "quantity": 1, "unit_price": str(amount),
                        "checked_at_utc": result["checked_at_utc"]})
    if len(choices) != 1:
        raise StopCheckout("没有唯一可验证的目标 SKU")
    return choices[0]


def validate_cart(items, candidate):
    if len(items) != 1:
        raise StopCheckout("购物袋必须只有目标商品；不会删除或购买原有商品")
    actual = items[0]
    for key in ("sku", "product_id", "product_name", "quantity", "color_name", "size"):
        expected = {"sku": str(candidate["sku"]), "product_id": PRODUCT_ID,
                    "product_name": PRODUCT_NAME, "quantity": 1,
                    "color_name": candidate["color"]["name"], "size": "6-8"}[key]
        if actual.get(key) != expected:
            raise StopCheckout(f"购物袋 {key} 与目标不符")


def validate_card(masked_summary):
    text = " ".join(masked_summary.split())
    if re.search(r"paypal|afterpay|amex|american express", text, re.I):
        raise StopCheckout("付款方式不是指定的 Mastercard")
    if not re.search(r"master\s*card", text, re.I):
        raise StopCheckout("未能确认 Mastercard")
    # Require a masked/ending marker, not an unrelated 0000 elsewhere in the page.
    if not re.search(r"(?:ending(?:\s+in)?|ends\s+in|[•●·*xX]{2,})\s*0000\b", text, re.I):
        raise StopCheckout("付款摘要未确认卡尾号 0000")
    other = re.findall(r"(?:ending(?:\s+in)?|ends\s+in|[•●·*xX]{2,})\s*(\d{4})\b", text, re.I)
    if any(x != CARD_LAST4 for x in other):
        raise StopCheckout("付款摘要包含其他银行卡")
    return CARD_LAST4


def allow_click(label):
    """Only these non-final buttons may be clicked by this test build."""
    normalized = " ".join(label.split()).casefold()
    if re.fullmatch(r"add to bag\s+a\$[\d,]+(?:\.\d{2})?", normalized):
        return True
    if normalized not in {"add to bag", "view bag", "view my bag", "checkout", "check out",
                          "continue", "continue to shipping", "continue to payment",
                          "continue to review", "enter address manually", "stay in australia",
                          "open search", "close"}:
        raise StopCheckout(f"测试版禁止点击此按钮：{label}")
    return True
