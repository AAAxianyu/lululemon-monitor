import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lululemon_checkout as runner


class RunnerRoutingTests(unittest.TestCase):
    def test_iab_handoff_writes_candidate_without_opening_chrome(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(runner, 'OUTPUT', Path(root)), patch.object(runner, 'publish') as publish, patch.object(runner, 'notify'):
                runner.prepare_checkout({'sku': '123456789'})
                self.assertEqual(json.loads((Path(root)/'candidate.json').read_text())['sku'], '123456789')
                self.assertEqual(publish.call_args.args[0], 'awaiting_iab_executor')

    def test_prepare_browser_defaults_to_iab_without_launching_chrome(self):
        with patch('sys.argv', ['lululemon_checkout.py', '--prepare-browser']), \
                patch.object(runner, 'exclusive_run', contextlib.nullcontext), \
                patch.object(runner, 'publish') as publish, patch.object(runner, 'browser_prepare') as chrome:
            self.assertEqual(runner.main(), 0)
            chrome.assert_not_called()
            self.assertEqual(publish.call_args.args[0], 'iab_session_required')

    def test_legacy_chrome_requires_explicit_option(self):
        with patch('sys.argv', ['lululemon_checkout.py', '--prepare-browser', '--browser', 'chrome']), \
                patch.object(runner, 'exclusive_run', contextlib.nullcontext), patch.object(runner, 'browser_prepare') as chrome:
            self.assertEqual(runner.main(), 0)
            chrome.assert_called_once()


if __name__ == '__main__':
    unittest.main()
