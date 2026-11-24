import copy
import datetime as dt
import unittest

from checkout_guards import StopCheckout, allow_click, select_candidate, validate_card, validate_cart
from lululemon_monitor import PRODUCT_ID, PRODUCT_NAME


def fixture():
    return {"product_id": PRODUCT_ID, "product_name": PRODUCT_NAME,
            "status": "requested_size_available", "has_request_error": False,
            "checked_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "requested_us_size": "XS", "requested_size_keys": ["4"],
            "target_skus": [{"sku": "123456789", "matches_requested_us_size": True,
                             "color": {"id": "77631", "name": "Lavender Rock", "colorGroup": "LAVR"},
                             "size_token": "4", "au_size": "6-8", "is_available": True,
                             "is_final_sale": False, "stock_source": "availability_endpoint",
                             "price": {"currencyCode": "AUD", "listPrice": 169}}]}


class CheckoutGuardsTests(unittest.TestCase):
    def test_exact_identity_only(self):
        good = fixture()
        self.assertEqual(select_candidate(good)["quantity"], 1)
        for key, value in [("product_id", "oversized"),
                           ("product_name", "Scuba Evolve Oversized Full-Zip Hoodie"),
                           ("requested_us_size", "S"), ("has_request_error", True)]:
            bad = copy.deepcopy(good)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(StopCheckout):
                select_candidate(bad)

    def test_colour_conflicts_and_combinations_stop(self):
        for change in ({"name": "Perfect Plumful/Gold"}, {"colorGroup": "PPGF"},
                       {"name": "Lavender Mist/Gold"}, {"id": "abc"}):
            bad = fixture()
            bad["target_skus"][0]["color"].update(change)
            with self.subTest(change=change), self.assertRaises(StopCheckout):
                select_candidate(bad)

    def test_lavender_rock_zip_combinations_allowed(self):
        for change in ({"name": "Lavender Rock/Gold", "id": "81999", "colorGroup": "LAVR/GOLD"},
                       {"name": "Lavender Rock/Mirror Silver", "id": "81998", "colorGroup": "LAVR/MRRS"}):
            ok = fixture()
            ok["target_skus"][0]["color"].update(change)
            with self.subTest(change=change):
                self.assertEqual(select_candidate(ok)["color"]["name"], change["name"])

    def test_stale_or_multiple_candidates_stop(self):
        bad = fixture()
        bad["checked_at_utc"] = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=3)).isoformat()
        with self.assertRaises(StopCheckout):
            select_candidate(bad)
        bad = fixture()
        bad["target_skus"].append(copy.deepcopy(bad["target_skus"][0]))
        with self.assertRaises(StopCheckout):
            select_candidate(bad)

    def test_absent_is_not_an_order_candidate(self):
        result = fixture()
        result.update(status="not_in_catalog", target_skus=[])
        self.assertIsNone(select_candidate(result))

    def test_unverified_stock_size_sale_and_currency_stop(self):
        for key, value in [("stock_source", "unknown"), ("au_size", "8-10"),
                           ("is_final_sale", True), ("is_final_sale", None),
                           ("price", {"currencyCode": "USD", "listPrice": 169}),
                           ("price", {"currencyCode": "AUD", "listPrice": "NaN"})]:
            bad = fixture()
            bad["target_skus"][0][key] = value
            with self.subTest(key=key), self.assertRaises(StopCheckout):
                select_candidate(bad)

    def test_wrong_card_or_paypal_rejected(self):
        for text in ("Mastercard ending in 0000", "MasterCard •••• 0000", "MasterCard ···· 0000"):
            self.assertEqual(validate_card(text), "0000")
        for text in ("Mastercard ending in 8888", "PayPal 0000", "Mastercard 0000",
                     "Visa ending in 0000", "Mastercard •••• 0000 •••• 8888"):
            with self.subTest(text=text), self.assertRaises(StopCheckout):
                validate_card(text)

    def test_cart_must_contain_only_confirmed_item(self):
        candidate = select_candidate(fixture())
        item = {"sku": candidate["sku"], "product_id": PRODUCT_ID,
                "product_name": PRODUCT_NAME, "quantity": 1,
                "color_name": "Lavender Rock", "size": "6-8"}
        validate_cart([item], candidate)
        for items in ([], [item, item], [{**item, "quantity": 2}], [{**item, "sku": "different"}],
                      [{**item, "color_name": "Black"}], [{**item, "size": "8-10"}]):
            with self.subTest(items=items), self.assertRaises(StopCheckout):
                validate_cart(items, candidate)

    def test_final_payment_actions_never_allowed(self):
        for label in ("Place order", "Place Order", "Pay now", "Submit order", "Buy now",
                      "Continue and pay", "Checkout with PayPal"):
            with self.subTest(label=label), self.assertRaises(StopCheckout):
                allow_click(label)
        self.assertTrue(allow_click("Continue"))


if __name__ == "__main__":
    unittest.main()
