# lululemon-monitor

Restock / launch monitor for a single lululemon AU product colour and size, with email alerts.

- `lululemon_monitor.py` – polls the AU product API and SKU availability endpoint (curl_cffi, Chrome impersonation).
- `availability_notifier.py` – detection loop (0.1 s cadence, two-step confirmation, 429/403 backoff) writing `listed` / `xs_available` events to an SQLite outbox.
- `smtp_notifier.py` – sends each event to the configured recipients over SMTP (credential kept in the macOS Keychain).
- `notification_chain.py` – supervisor that runs detector, mailer and (optional) browser worker; `start | resume | stop | status`.
- `direct_checkout.mjs`, `iab_checkout.mjs`, `native_shipping.py`, … – optional browser automation that adds the item to the bag and stops at the payment-entry page. It never fills card details and never places an order.
- `investigation/` – raw API captures from the initial investigation (cookie values removed).

Personal details (email addresses, name, address, phone, card tail) have been replaced with example values. Set your own before use.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-monitor.txt
.venv/bin/python -m unittest
```
