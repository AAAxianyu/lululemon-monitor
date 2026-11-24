import copy
import json
from pathlib import Path
import unittest
from lululemon_monitor import analyze


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.product = json.loads((Path(__file__).parent / "investigation/au_product_api.pretty.json").read_text())

    def add_target(self):
        a = self.product["data"]["attributes"]
        color = {"id": "77631", "name": "Lavender Rock", "colorGroup": "LAVR"}
        a["colors"].append(color)
        sku = copy.deepcopy(next(s for s in a["skus"] if s["sizeTokenKey"] == "4"))
        sku.update(id="test-target", color=color)
        a["skus"].append(sku)
        return {"data": [{"id": "test-target", "type": "skus.availability", "attributes": {
            "isAvailable": True, "isLowStock": True, "quantity": 2, "fulfillmentType": "ship"}}]}

    def test_real_snapshot_absence(self):
        self.assertEqual(analyze(self.product, None)["status"], "not_in_catalog")

    def test_badge_does_not_count_as_sku(self):
        self.product["data"]["attributes"]["newColorsBadging"] = ["077631"]
        result = analyze(self.product, None)
        self.assertTrue(result["badge_only_hint"])
        self.assertEqual(result["status"], "not_in_catalog")

    def test_xs_mapping_and_stock(self):
        result = analyze(self.product, self.add_target())
        self.assertEqual(result["requested_size_keys"], ["4"])
        self.assertEqual(result["status"], "requested_size_available")
        self.assertEqual(result["target_skus"][0]["au_size"], "6-8")
        self.assertEqual(result["target_skus"][0]["reported_quantity"], 2)

    def test_stock_failure_not_sold_out_or_cached_available(self):
        self.add_target()
        self.assertEqual(analyze(self.product, None)["status"], "stock_unknown")
        self.assertEqual(analyze(self.product, {"data": []})["status"], "stock_unknown")

    def test_other_size_stock_not_xs(self):
        stock = self.add_target()
        self.product["data"]["attributes"]["skus"][-1]["sizeTokenKey"] = "8"
        self.assertEqual(analyze(self.product, stock)["status"], "requested_size_not_listed")

    def test_oversized_rejected(self):
        self.product["data"]["attributes"]["name"] = "Scuba Evolve Oversized Full-Zip Hoodie"
        with self.assertRaises(ValueError):
            analyze(self.product, None)


if __name__ == "__main__":
    unittest.main()
