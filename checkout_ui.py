"""AU storefront adapter. This module has NO final-order action."""
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from checkout_guards import StopCheckout, allow_click, validate_card, validate_cart
from lululemon_monitor import BASE, PRODUCT_ID, PRODUCT_NAME

BAG_URL = BASE + "/en-au/shop/mybag"


def normalized_bag(payload):
    """Keep only merchandise fields from the page's own bag response."""
    if not isinstance(payload, dict):
        return None
    bag = payload.get("bag")
    if isinstance(bag, dict) and isinstance(bag.get("items"), dict):
        groups = bag["items"]
        if not isinstance(groups.get("shipItems"), list) or not isinstance(groups.get("bopisItems"), list):
            return None
        if bag.get("errorCode"):
            return None
        all_items = groups["shipItems"] + groups["bopisItems"]
        if groups.get("totalLineItemCount") != len(all_items):
            return None
        rows = []
        for item in all_items:
            sku = item.get("selectedSku") or {}
            color = sku.get("color") or {}
            raw_size = sku.get("size")
            size = "6-8" if raw_size in ("6-8", "US XS (AU 6-8)") else raw_size
            rows.append({"sku": str(sku.get("id", "")), "product_id": item.get("productId"),
                         "product_name": item.get("productName"), "quantity": item.get("quantity"),
                         "color_name": color.get("name"), "size": size,
                         "image": sku.get("image"), "is_bopis": item.get("isBopis"),
                         "is_final_sale": item.get("isFinalSale")})
        return rows
    for value in payload.values():
        if isinstance(value, dict):
            found = normalized_bag(value)
            if found is not None:
                return found
    return None


class CheckoutUI:
    def __init__(self, page, publish, shipping, output, manual_wait_seconds=0, notify=None):
        self.page = page
        self.publish = publish
        self.shipping = shipping
        self.output = Path(output)
        self.manual_wait_seconds = manual_wait_seconds
        self.notify = notify or (lambda message: None)
        self.items = None
        self.bag_updated = 0.0
        self.api_failure = None
        page.on("response", self._response)
        page.set_default_timeout(12000)

    def _response(self, response):
        parsed = urlparse(response.url)
        if parsed.hostname != "www.lululemon.com.au" or "/graphql" not in parsed.path:
            return
        if response.status != 200:
            code = None
            operation = None
            try:
                error_body = response.json()
                value = error_body.get("errorCode") if isinstance(error_body, dict) else None
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
                    code = value
                request_body = response.request.post_data_json
                value = request_body.get("operationName") if isinstance(request_body, dict) else None
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value):
                    operation = value
            except Exception:
                pass
            suffix = f"（{code}）" if code else ""
            self.api_failure = f"澳洲官网 {parsed.path} 返回 HTTP {response.status}{suffix}；程序已停止"
            self.publish("api_error", method=response.request.method, path=parsed.path,
                         status_code=response.status, error_code=code, operation=operation)
            return
        try:
            body = response.json()
            if body.get("errors"):
                return
            rows = normalized_bag(body)
            if rows is not None:
                self.items = rows
                self.bag_updated = time.monotonic()
        except Exception:
            # Unknown/failed responses do not change the last observation timestamp.
            return

    def assert_site(self):
        parsed = urlparse(self.page.url)
        if parsed.scheme != "https" or parsed.hostname != "www.lululemon.com.au":
            raise StopCheckout("浏览器离开澳洲官网，停止操作")

    def click(self, locator):
        self.assert_site()
        if "/checkout/review" in self.page.url:
            raise StopCheckout("测试版在 Review 页面禁止自动点击")
        if locator.count() != 1:
            raise StopCheckout("按钮不唯一，停止")
        label = locator.get_attribute("aria-label") or locator.inner_text().strip()
        allow_click(label)
        if locator.get_attribute("data-testid") == "place-order-button":
            raise StopCheckout("测试版禁止提交订单")
        locator.click()

    def wait_until(self, condition, message, seconds=35):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.api_failure:
                raise StopCheckout(self.api_failure)
            if condition():
                return
            # Playwright's wait dispatches pending network/DOM events.
            self.page.wait_for_timeout(250)
        raise StopCheckout(message)

    def stay_in_australia(self):
        button = self.page.get_by_role("button", name="Stay in Australia", exact=True)
        if button.count() == 1 and button.is_visible():
            self.click(button)
        welcome = self.page.get_by_test_id("welcome-offer-modal")
        if welcome.count() == 1 and welcome.is_visible():
            close = welcome.get_by_role("button", name="Close", exact=True)
            if close.count() == 1:
                self.click(close)

    def open_product(self):
        """Use the site's normal client navigation; direct PDP GET can be denied."""
        if (f"/p/scuba-evolve-classic-full-zip-hoodie/{PRODUCT_ID}" in self.page.url
                and self.page.get_by_role("heading", name=PRODUCT_NAME, exact=True).count()):
            return
        # This search URL was observed from the site's own search form.
        self.page.goto(BASE + "/en-au/search?Ntt=Scuba%20Evolve%20Classic", wait_until="load")
        self.stay_in_australia()
        link = self.page.get_by_role("link", name=PRODUCT_NAME, exact=True).filter(
            has_text=re.compile("^" + re.escape(PRODUCT_NAME) + "$"))
        link.wait_for(timeout=25000)
        self.stay_in_australia()
        href = link.get_attribute("href") or ""
        if urlparse(href).path != f"/en-au/p/scuba-evolve-classic-full-zip-hoodie/{PRODUCT_ID}":
            raise StopCheckout("搜索结果商品 ID 不符")
        link.click()
        self.page.wait_for_url(f"**/p/scuba-evolve-classic-full-zip-hoodie/{PRODUCT_ID}**", timeout=25000)

    def bag(self):
        since = time.monotonic()
        self.items = None
        self.page.goto(BAG_URL, wait_until="domcontentloaded")
        self.stay_in_australia()
        empty = self.page.get_by_test_id("bag-empty-container")
        tiles = self.page.get_by_test_id("product-tile-actionable")
        self.wait_until(lambda: empty.is_visible() or tiles.count() > 0,
                        "购物袋未正常加载；可能需要登录或处理网站验证")
        if empty.is_visible():
            if self.items not in (None, []):
                raise StopCheckout("购物袋页面与接口内容不一致")
            return []
        self.wait_until(lambda: self.items is not None and self.bag_updated >= since,
                        "未取得当前购物袋 SKU 数据，停止")
        if len(self.items) != tiles.count():
            raise StopCheckout("购物袋页面与接口数量不一致")
        return self.items

    def confirm_visible_items(self, candidate):
        validate_cart(self.items or [], candidate)
        if any(x.get("is_bopis") is True for x in self.items):
            raise StopCheckout("商品为门店自提，不是配送")
        if any(x.get("is_final_sale") is True for x in self.items):
            raise StopCheckout("购物袋标记 Final Sale，停止")
        # The DOM must agree with the normalized SKU response.
        tiles = self.page.get_by_test_id("product-tile-actionable")
        visible = [tile for tile in tiles.all() if tile.is_visible()]
        if len(visible) != 1:
            raise StopCheckout("无法在页面中唯一确认目标商品")
        tile = visible[0]
        if tile.get_by_test_id("bag-product-name").inner_text().strip() != PRODUCT_NAME:
            raise StopCheckout("页面商品名称不符")
        if tile.get_by_test_id("bag-product-color").inner_text().strip() != candidate["color"]["name"]:
            raise StopCheckout("页面商品颜色不符")
        size = tile.get_by_test_id("bag-product-size").inner_text().strip()
        if not re.search(r"(?:^|\s)(?:6\s*[-–]\s*8|XS)(?:$|\s|\))", size):
            raise StopCheckout("页面尺码不符")
        quantity = tile.get_by_test_id("quantity-value")
        if quantity.count() == 0:
            quantity = tile.get_by_test_id("bag-item-cost-label-value")
        if quantity.count() != 1 or quantity.inner_text().strip() != "1":
            raise StopCheckout("页面数量未确认是 1 件")
        return tile

    def add_candidate(self, candidate):
        # Never depend on a URL colour override alone.
        self.open_product()
        self.stay_in_australia()
        self.page.get_by_role("heading", name=PRODUCT_NAME, exact=True).wait_for()
        add = self.page.get_by_role("button", name=re.compile(r"^Add to bag(?:\s+A\$[\d,.]+)?$", re.I))
        add.wait_for()
        color = self.page.get_by_role("radio", name=candidate["color"]["name"], exact=True)
        color.wait_for()
        if color.count() != 1:
            raise StopCheckout("无法唯一定位目标颜色选项")
        if not color.is_checked():
            color.press("Space")
        self.wait_until(lambda: color.is_checked(), "目标颜色未选中")
        size = self.page.get_by_test_id("size-selector-alpine").get_by_role(
            "radio", name="Extra small, US, AU 6 to 8", exact=True)
        size.wait_for()
        if not size.is_checked():
            size.press("Space")
        self.wait_until(lambda: size.is_checked(), "US XS 未选中")
        sku_pattern = re.compile(r"^SKU\s*[-:]?\s*" + re.escape(str(candidate["sku"])) + r"$", re.I)
        self.wait_until(lambda: self.page.get_by_text(sku_pattern).count() == 1,
                        "商品页未显示已选择的目标 SKU，停止加购")
        self.publish("adding_to_bag", candidate=candidate)
        # One attempt only: a timeout cannot cause a second add.
        self.click(add)
        self.wait_until(lambda: self.page.get_by_text(re.compile(r"^Added to Bag", re.I)).count() > 0,
                        "加购结果不明确，停止；不会重复加购")
        validate_cart(self.bag(), candidate)
        self.confirm_visible_items(candidate)

    def contact(self):
        field = self.page.get_by_test_id("contact-email-input").locator("input")
        field.wait_for()
        if not self.shipping.get("email"):
            raise StopCheckout("缺少联系邮箱")
        field.fill(self.shipping["email"])
        self.uncheck_optional("#signup-checkbox")
        self.click(self.page.get_by_test_id("contact-continue-button"))
        self.wait_until(lambda: "/checkout/shipping" in self.page.url,
                        "联系方式未通过官网校验")

    def uncheck_optional(self, selector):
        checkbox = self.page.locator(selector)
        if checkbox.count() == 1 and checkbox.is_checked():
            checkbox.press("Space")
            if checkbox.is_checked():
                raise StopCheckout("未能取消保存或订阅选项")

    def shipping_form(self):
        form = self.page.get_by_test_id("shipping-form")
        form.wait_for()
        manual = self.page.get_by_role("button", name="Enter address manually", exact=True)
        if manual.count() == 1 and manual.is_visible():
            self.click(manual)
        for name in ("firstName", "lastName", "address1", "address2", "city", "postalCode", "phone"):
            value = self.shipping.get(name)
            if value is None:
                raise StopCheckout(f"收货信息缺少 {name}")
            field = form.locator(f'input[name="{name}"]')
            if field.count() != 1:
                raise StopCheckout(f"无法定位收货字段 {name}，请在浏览器补全")
            field.fill(value)
        state = form.locator('select[name="state"]')
        if state.count() != 1:
            raise StopCheckout("无法定位州选择框，请在浏览器补全")
        state.select_option(self.shipping["state"])
        self.uncheck_optional("#save-to-profile")
        self.uncheck_optional('input[name="authorityToLeave"]')
        self.click(self.page.get_by_test_id("shipping-section-submit-button"))
        self.wait_until(lambda: "/checkout/payment" in self.page.url,
                        "地址或配送方式需要人工核对，未进入付款页")

    def payment(self):
        # Only a saved Mastercard with the requested suffix may be selected.
        self.page.get_by_test_id("section-payment").wait_for()
        saved = self.page.locator('[data-testid^="saved-card-last4-"]')
        matches = [x for x in saved.all() if x.inner_text().strip().endswith("0000")]
        if len(matches) == 1:
            suffix = matches[0].get_attribute("data-testid").removeprefix("saved-card-last4-")
            details = self.page.get_by_test_id("saved-card-details-" + suffix)
            summary = details.inner_text()
            validate_card(summary)
            radio = self.page.get_by_test_id("saved-card-radio-" + suffix)
            radio.check()
            button = self.page.get_by_test_id("saved-card-continue-button")
            if button.count() == 1 and button.is_enabled():
                self.click(button)
                self.page.wait_for_timeout(500)
        if "/checkout/review" not in self.page.url:
            message = "需要你在官网填写或补全 Bybit 0000（可能包括安全码），并点 Continue；程序不读取完整卡号"
            self.publish("needs_payment_details", message=message)
            self.notify(message)
            self.wait_until(lambda: "/checkout/review" in self.page.url, message,
                            seconds=self.manual_wait_seconds)

    def review(self, candidate):
        self.assert_site()
        self.page.get_by_test_id("place-order-button").wait_for()
        self.wait_until(lambda: self.items is not None, "无法核对最终购物袋")
        tile = self.confirm_visible_items(candidate)
        payment = self.page.get_by_test_id("payment-summary-methods")
        payment.wait_for()
        validate_card(payment.inner_text())
        if self.page.get_by_test_id("payment-summary-gift-cards").count():
            raise StopCheckout("订单含其他支付方式，停止")
        self.output.mkdir(parents=True, exist_ok=True)
        tile.screenshot(path=str(self.output / "selected-product.png"))
        payment.screenshot(path=str(self.output / "selected-card.png"))
        self.publish("ready_before_place_order", candidate=candidate, card_last4="0000",
                     message="已核对目标和卡尾号；测试版停在 Place order 前，没有提交订单")
        # No click, no checkbox acceptance, no Enter key, no request to the order endpoint.

    def run(self, candidate, *, resume=False):
        if resume and "/checkout/review" in self.page.url:
            self.page.reload(wait_until="domcontentloaded")
            return self.review(candidate)
        items = self.bag()
        if items:
            validate_cart(items, candidate)
        else:
            if resume:
                raise StopCheckout("恢复时购物袋为空，不会重新加购")
            self.add_candidate(candidate)
        self.confirm_visible_items(candidate)
        self.click(self.page.get_by_test_id("bag-proceed-checkout-button"))
        self.wait_until(lambda: "/shop/checkout" in self.page.url, "未进入结账页")
        self.publish("checkout_in_progress", candidate=candidate)
        if self.page.url.rstrip("/").endswith("/shop/checkout"):
            self.contact()
        if "/checkout/shipping" in self.page.url:
            self.shipping_form()
        if "/checkout/payment" in self.page.url:
            self.payment()
        if "/checkout/review" not in self.page.url:
            raise StopCheckout("结账页面状态未知，停止")
        self.review(candidate)
