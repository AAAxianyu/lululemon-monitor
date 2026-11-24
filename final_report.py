#!/usr/bin/env python3
"""One-shot end-of-window report (user request 2026-11-17 01:2x CST).

At 06:00:30 Beijing time: if no listed/xs_available event was recorded, email the 163
address only, saying nothing was detected and that a script fault cannot be ruled out.
Sends at most once (guard file). Uses the existing QQ SMTP config and Keychain credential.
"""
import datetime as dt
import json
import os
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime

from availability_notifier import DATA, RAW, END, BEIJING, Outbox, utcnow
from smtp_notifier import configuration, keychain, write_json

RECIPIENT = 'recipient2@example.com'
FIRE_AT = END + dt.timedelta(seconds=30)
GUARD = DATA / 'final-report.json'


def run_stats():
    total = failed = 0
    last_ok = last_status = None
    path = RAW / 'checks.jsonl'
    if path.exists():
        with path.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get('checked_at_utc', '') < '2026-11-16T16:50':
                    continue
                total += 1
                if row.get('has_request_error') or row.get('status') == 'query_failed':
                    failed += 1
                else:
                    last_ok, last_status = row.get('checked_at_utc'), row.get('status')
    return total, failed, last_ok, last_status


def main():
    while utcnow() < FIRE_AT:
        time.sleep(min(30, max(1, (FIRE_AT - utcnow()).total_seconds())))
    if GUARD.exists():
        print('already attempted'); return
    stages = Outbox().stages()
    if {'listed', 'xs_available'} & stages:
        write_json(GUARD, {'stage': 'skipped_target_detected', 'stages': sorted(stages), 'at_utc': utcnow().isoformat()})
        return
    total, failed, last_ok, last_status = run_stats()
    to_cst = lambda s: dt.datetime.fromisoformat(s).astimezone(BEIJING).strftime('%H:%M:%S') if s else '无'
    body = (
        '你好，\n\n'
        '截至北京时间 06:00，脚本还没有检测到 Scuba Evolve Classic 的 Lavender Rock 在澳洲官网上线。\n\n'
        '不过目前还不能确定是真的还没上线，还是脚本那边出了问题。方便的时候可以到官网看一眼确认一下。\n'
    )
    config = configuration()
    message = EmailMessage(policy=SMTP)
    message['Subject'] = '[lululemon 监控结束] 截至 06:00 未检测到 Classic Lavender Rock 上线'
    message['From'], message['To'] = config['account'], RECIPIENT
    message['Date'] = format_datetime(utcnow())
    message.set_content(body)
    record = {'stage': 'sending', 'recipient': RECIPIENT, 'checks': total, 'failed': failed, 'at_utc': utcnow().isoformat()}
    write_json(GUARD, record)
    try:
        with smtplib.SMTP_SSL(config['host'], 465, context=ssl.create_default_context(), timeout=30) as client:
            client.login(config['account'], keychain(config['account']))
            refused = client.send_message(message, from_addr=config['account'], to_addrs=[RECIPIENT])
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        record['stage'] = 'accepted'
    except Exception as error:
        record.update(stage='failed', error_type=type(error).__name__)
    record['finished_at_utc'] = utcnow().isoformat()
    write_json(GUARD, record)
    print(json.dumps(record, ensure_ascii=False))


if __name__ == '__main__':
    main()
