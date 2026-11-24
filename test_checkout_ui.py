"""Offline browser checks: every network request is fulfilled locally."""
import json
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright
from checkout_ui import CheckoutUI, normalized_bag
from checkout_guards import StopCheckout, select_candidate
from test_checkout import fixture


class CheckoutBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.candidate = select_candidate(fixture())
        self.events = []
        self.temp = tempfile.TemporaryDirectory()
        self.adds = 0
        self.final_attempts = 0
        self.last4 = "0000"
        self.context.route("**/*", self.route)
        self.ui = CheckoutUI(self.page, lambda stage, **kw: self.events.append(stage),
                             {"email": "test@example.invalid", "firstName": "Test", "lastName": "User",
                              "address1": "1 Test Road", "address2": "", "city": "Test",
                              "state": "NSW", "postalCode": "2020", "phone": "0400000000"}, self.temp.name)

    def tearDown(self):
        self.context.close()
        self.temp.cleanup()

    def row(self):
        return {"productId": self.candidate["product_id"], "productName": self.candidate["product_name"],
                "quantity": 1, "isBopis": False, "isFinalSale": False,
                "selectedSku": {"id": self.candidate["sku"], "size": "US XS (AU 6-8)",
                                "color": {"name": "Lavender Rock"}, "image": None}}

    def tile(self):
        return f'''<div data-testid="product-tile-actionable">
        <span data-testid="bag-product-name">{self.candidate['product_name']}</span>
        <span data-testid="bag-product-color">Lavender Rock</span>
        <span data-testid="bag-product-size">Size 6-8</span><span data-testid="quantity-value">1</span></div>'''

    def route(self, route):
        path = route.request.url.split(".com.au", 1)[-1].split("?", 1)[0]
        if path == "/api/graphql":
            rows = [self.row()] if self.adds else []
            route.fulfill(json={"data": {"bag": {"errorCode": None, "items": {
                "shipItems": rows, "bopisItems": [], "totalLineItemCount": len(rows)}}}})
            return
        if path == "/test-add":
            self.adds += 1
            route.fulfill(json={"ok": True})
            return
        if path == "/test-place-order":
            self.final_attempts += 1
            route.fulfill(status=500)
            return
        script = '<script>fetch("/api/graphql")</script>'
        if path.endswith("/home"):
            html = '''<button aria-label="Open Search">Search</button><input type="search" aria-label="Search" onkeydown="if(event.key==='Enter') location.href='/en-au/search?Ntt=Scuba'"><script></script>'''
        elif path.endswith("/search"):
            html = f'''<a href="/en-au/p/scuba-evolve-classic-full-zip-hoodie/{self.candidate['product_id']}?fp=1&color=77631">{self.candidate['product_name']}</a>'''
        elif "/en-au/p/" in path:
            html = f'''<h1>{self.candidate['product_name']}</h1>
            <input type="radio" aria-label="Lavender Rock" checked>
            <div data-testid="size-selector-alpine"><label><input type="radio" aria-label="Extra small, US, AU 6 to 8"><span>US XS / AU 6-8</span></label></div>
            <span>SKU - {self.candidate['sku']}</span><button aria-label="Add to bag A$169" onclick="fetch('/test-add').then(()=>document.querySelector('#added').textContent='Added to Bag')">Add to bag<br>A$169</button><div id="added"></div>'''
        elif path.endswith("/shop/mybag"):
            html = self.tile() + '<button data-testid="bag-proceed-checkout-button" onclick="location.href=\'/en-au/shop/checkout\'">Checkout</button>' if self.adds else '<div data-testid="bag-empty-container">Empty bag</div>'
        elif path.endswith("/checkout"):
            html = '<div data-testid="contact-email-input"><input type="email"></div><button data-testid="contact-continue-button" onclick="location.href=\'/en-au/shop/checkout/shipping\'">Continue</button>'
        elif path.endswith("/shipping"):
            html = '<form data-testid="shipping-form">' + ''.join(f'<input name="{key}">' for key in ("firstName", "lastName", "address1", "address2", "city", "postalCode", "phone")) + '<select name="state"><option value="NSW">NSW</option></select></form><button data-testid="shipping-section-submit-button" onclick="location.href=\'/en-au/shop/checkout/payment\'">Continue</button>'
        elif path.endswith("/payment"):
            html = f'''<section data-testid="section-payment"><div data-testid="saved-card-details-test">Mastercard **** <span data-testid="saved-card-last4-test">{self.last4}</span></div><input type="radio" data-testid="saved-card-radio-test"><button data-testid="saved-card-continue-button" onclick="location.href='/en-au/shop/checkout/review'">Continue</button></section>'''
        elif path.endswith("/review"):
            html = self.tile() + f'''<div data-testid="payment-summary-methods">Mastercard ···· {self.last4}</div><button data-testid="place-order-button" onclick="fetch('/test-place-order')">Place order</button>'''
        else:
            route.fulfill(status=404, body="offline fixture: unknown route")
            return
        route.fulfill(content_type="text/html; charset=utf-8", body='<meta charset="utf-8">'+html+script)

    def test_full_flow_stops_before_final_action(self):
        self.ui.run(self.candidate)
        self.assertEqual(self.adds, 1)
        self.assertEqual(self.final_attempts, 0)
        self.assertEqual(self.events[-1], "ready_before_place_order")
        self.assertTrue((Path(self.temp.name)/"selected-card.png").exists())

    def test_wrong_saved_card_does_not_continue(self):
        self.last4 = "8888"
        with self.assertRaises(StopCheckout):
            self.ui.run(self.candidate)
        self.assertTrue(self.page.url.endswith("/payment"))
        self.assertNotIn("ready_before_place_order", self.events)
        self.assertEqual(self.final_attempts, 0)

    def test_shipping_clears_authority_to_leave_before_continue(self):
        self.page.goto("https://www.lululemon.com.au/en-au/shop/checkout/shipping")
        self.page.evaluate("""() => {
            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.name = 'authorityToLeave';
            checkbox.checked = true;
            document.querySelector('form').appendChild(checkbox);
            document.querySelector('[data-testid="shipping-section-submit-button"]')
                .addEventListener('click', () => {
                    localStorage.setItem('testAuthorityToLeave', String(checkbox.checked));
                });
        }""")
        self.ui.shipping_form()
        self.assertEqual(self.page.evaluate("localStorage.getItem('testAuthorityToLeave')"), "false")
        self.assertEqual(self.final_attempts, 0)

    def test_wrong_card_on_review_never_ready(self):
        self.adds = 1
        self.last4 = "8888"
        self.page.goto("https://www.lululemon.com.au/en-au/shop/checkout/review")
        with self.assertRaises(StopCheckout):
            self.ui.review(self.candidate)
        self.assertEqual(self.final_attempts, 0)

    def test_shipping_http_400_stops_and_keeps_only_error_metadata(self):
        captured = []
        self.ui.publish = lambda stage, **details: captured.append((stage, details))
        response = SimpleNamespace(
            url="https://www.lululemon.com.au/api/graphql", status=400,
            request=SimpleNamespace(method="POST", post_data_json={
                "operationName": "setShippingInfo", "variables": {"private": "do-not-log"}}),
            json=lambda: {"message": "Bad Request.", "errorCode": "GE401001"})
        self.ui._response(response)
        with self.assertRaisesRegex(StopCheckout, "GE401001"):
            self.ui.wait_until(lambda: True, "should not continue")
        self.assertEqual(captured[0][1]["operation"], "setShippingInfo")
        self.assertNotIn("do-not-log", str(captured))
        self.assertEqual(self.final_attempts, 0)


if __name__ == "__main__":
    unittest.main()
