#!/usr/bin/env python3
"""Bounded, read-only AU API monitor and durable email outbox. No checkout actions.

Delivery uses an explicitly configured SMTP sender or the event-triggered Gmail connector.
"""
import argparse
import contextlib
import concurrent.futures as futures
import datetime as dt
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from lululemon_monitor import check, persist_result, lavender_family, target_color, PRODUCT_ID, PRODUCT_NAME, PRODUCT_URL, STOCK_URL, PAGE_URL
from monitor_control import is_paused

ROOT = Path(__file__).resolve().parent
DATA = ROOT / '.private' / 'availability-notifier'
RAW = ROOT / 'monitor_data' / 'notifications'
BEIJING = ZoneInfo('Asia/Shanghai')
START = dt.datetime.fromisoformat('2026-11-17T00:50:00+08:00')
# 2026-11-17 user request: keep checking until detected (1-year safety cap, not a real window).
END = dt.datetime.fromisoformat('2027-11-17T06:00:00+08:00')
RECIPIENT = 'alerts@example.com'
RECIPIENTS = (RECIPIENT, 'recipient2@example.com', 'recipient3@example.com')
WINDOW = '20261117-until-detected'
INTERVAL = 0.1
FAST_INTERVAL = 0.1
MAX_IN_FLIGHT = 10  # Two GETs per round, at most 20 HTTP requests in flight.
FAST_WINDOWS = ((START, END),)


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def scheduled_interval(now=None):
    return INTERVAL


def exact_color(color):
    """Checkout-eligible: Lavender Rock or Lavender Rock/<zip> (name and LAVR group agree)."""
    return lavender_family(color)


def classify(result):
    """Listing needs actual matching SKUs; availability additionally needs valid XS stock."""
    if result.get('status') == 'query_failed' or result.get('product_id') != PRODUCT_ID \
            or result.get('product_name') != PRODUCT_NAME or result.get('requested_us_size') != 'XS':
        return {'stage': 'query_failed'}
    # Notification is broad: any Lavender Rock / LAVR colour on this Classic product.
    rows = [r for r in result.get('target_skus', [])
            if target_color(r.get('color', {})) and re.fullmatch(r'\d+', str(r.get('sku', '')))]
    if not rows:
        return {'stage': 'not_listed', 'has_request_error': result.get('has_request_error', False)}
    xs = [r for r in rows if r.get('matches_requested_us_size') is True
          and str(r.get('size_token')) in result.get('requested_size_keys', [])
          and r.get('au_size') == '6-8']
    available = [r for r in xs if r.get('is_available') is True
                 and r.get('stock_source') == 'availability_endpoint']
    # Conflicting/ambiguous inventory is not advertised as the requested size being buyable.
    ready = (len(xs) == 1 and len(available) == 1 and exact_color(xs[0].get('color', {}))
             and not result.get('has_request_error'))
    return {'stage': 'xs_available' if ready else 'listed', 'rows': rows, 'xs': xs,
            'checked_at_utc': result.get('checked_at_utc'),
            'has_request_error': result.get('has_request_error', False)}


def backoff(result, failures, minimum=INTERVAL, now=None):
    now = now or utcnow()
    # A transient failure never causes rapid retries, even during a subsecond peak.
    delay = min(max(30, minimum) * 2 ** min(failures - 1, 6), 900) if failures else minimum
    for request in result.get('requests', []):
        raw = (request.get('response_headers') or {}).get('retry-after')
        if not raw:
            continue
        try:
            value = float(raw) if str(raw).isdigit() else (parsedate_to_datetime(raw)-now).total_seconds()
            delay = max(delay, value)
        except (ValueError, TypeError, OverflowError):
            pass
    return max(minimum, delay)


def next_sleep(result, failures, elapsed, now=None):
    now = now or utcnow()
    delay = backoff(result, failures, minimum=scheduled_interval(now), now=now)
    if failures:
        # Retry-After is counted from receiving the response, not from request start.
        sleep = delay
    else:
        sleep = max(0, delay - elapsed)
        future_boundaries = [point for window in FAST_WINDOWS for point in window if point > now]
        if future_boundaries:
            sleep = min(sleep, (min(future_boundaries) - now).total_seconds())
    return max(0, min(sleep, (END - now).total_seconds()))


def mail_for(observation, event_id, *, control=None):
    tested = control is not None
    stamp = utcnow().astimezone(BEIJING).isoformat(timespec='seconds')
    ready = observation.get('stage') == 'xs_available'
    label = '测试邮件，不代表 Lavender Rock 上架' if tested else (
        'Classic Lavender Rock：US XS 库存接口显示有货' if ready else
        'Classic Lavender Rock 已出现商品 SKU，US XS 尚未确认有货')
    rows = observation.get('xs') or observation.get('rows') or []
    skus = ', '.join(str(r['sku']) for r in rows[:12]) or '未确认'
    source_link = PAGE_URL
    if not tested and rows:
        source_link += '?color=' + str(rows[0]['color'].get('id','')) + '&skuId=' + str(rows[0]['sku'])
    paragraphs = [f'<h2>{html.escape(label)}</h2>',
                  f'<p>目标：{html.escape(PRODUCT_NAME)}<br>Product ID：{PRODUCT_ID}<br>'
                  '颜色：' + html.escape(', '.join(sorted({f"{r['color'].get('name')} / {r['color'].get('colorGroup')} / {r['color'].get('id')}" for r in rows})) or 'Lavender Rock（含拉链组合色）') + '<br>目标尺码：US XS（AU 6–8），1 件</p>',
                  f'<p>检测时间（北京时间）：{stamp}<br>相关 SKU：{html.escape(skus)}</p>']
    if tested:
        paragraphs.append('<p><strong>本封只测试通知链路；对照商品是已有的 Classic Black / US XS，'
                          '不是目标 Lavender Rock，不会加购或购买。</strong></p>')
        paragraphs.append('<pre>' + html.escape(json.dumps(control, ensure_ascii=False, indent=2)) + '</pre>')
    else:
        status = ('官网库存接口确认目标 US XS 可售。' if ready else
                  '官网商品接口存在精确颜色的实际 SKU；目标 US XS 的库存尚未确认，请勿当作已能购买。')
        paragraphs.append('<p>' + status + '</p>')
        if observation.get('has_request_error'):
            paragraphs.append('<p>库存查询有错误；这不是“售罄”的证据。</p>')
    paragraphs.extend([f'<p><a href="{html.escape(source_link, quote=True)}">查看澳洲官网商品页</a>（请核对实际颜色和尺码）</p>',
        f'<p>证据：<a href="{PRODUCT_URL}">商品接口</a> · <a href="{STOCK_URL}">配送库存接口</a></p>',
        '<p>库存是查询时的快照，不代表已锁定库存或能够完成下单。</p>',
        f'<p>监控方式：持续检测直到上架。通知编号：{event_id}</p>'])
    return {'to': RECIPIENT if tested else ', '.join(RECIPIENTS), 'subject': f'[{event_id}] {label}',
            'payload': {'mime_type': 'text/html', 'charset': 'UTF-8', 'body': {'content': '\n'.join(paragraphs)}}}


class Outbox:
    def __init__(self, directory=DATA):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.dir.chmod(0o700)
        self.db_path = self.dir / 'outbox.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, stage TEXT NOT NULL, '
                       'status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, attempts INTEGER NOT NULL, '
                       'mail TEXT NOT NULL, provider_id TEXT, lease_token TEXT)')
            if 'evidence' not in {row['name'] for row in db.execute('PRAGMA table_info(events)')}:
                try:
                    db.execute('ALTER TABLE events ADD COLUMN evidence TEXT')
                except sqlite3.OperationalError as error:
                    if 'duplicate column' not in str(error).lower():
                        raise
        self.db_path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def stages(self):
        with self.connect() as db:
            return {r['stage'] for r in db.execute('SELECT stage FROM events')}

    def enqueue(self, observation, *, control=None):
        stage = 'test' if control is not None else observation['stage']
        if stage not in ('test', 'listed', 'xs_available'):
            return None
        event_id = 'LULU-' + hashlib.sha256(f'{WINDOW}:{stage}'.encode()).hexdigest()[:16]
        mail = mail_for(observation, event_id, control=control)
        now = time.time()
        with self.connect() as db:
            cursor = db.execute('INSERT OR IGNORE INTO events '
                               '(id,stage,status,created,updated,attempts,mail,provider_id,lease_token,evidence) '
                               'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               (event_id, stage, 'pending', now, now, 0, json.dumps(mail, ensure_ascii=False),
                                None, None, json.dumps(observation,ensure_ascii=False)))
        return event_id if cursor.rowcount else None

    def pending(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE status='pending' OR (status='sending' AND updated<?) ORDER BY created", (time.time()-120,)).fetchall()
        return [{**{k:r[k] for k in ('id','stage','status','created','attempts')},
                 'requires_sent_search':r['status']=='sending', 'mail':json.loads(r['mail'])} for r in rows]

    def claim(self, event_id):
        import secrets
        token = secrets.token_hex(16)
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            if row is None or row['status']=='sent' or (row['status']=='sending' and row['updated']>now-120):
                raise ValueError('Event is missing, sent, or currently leased')
            db.execute("UPDATE events SET status='sending',updated=?,attempts=attempts+1,lease_token=? WHERE id=?", (now,token,event_id))
        return {'id':event_id,'lease_token':token,'requires_sent_search':row['status']=='sending', 'mail':json.loads(row['mail'])}

    def sent(self, event_id, message_id, token=None):
        if not message_id or len(message_id)>256:
            raise ValueError('Actual Gmail message ID is required')
        with self.connect() as db:
            row=db.execute('SELECT * FROM events WHERE id=?',(event_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown event')
            if row['status']=='sent':
                return
            if token is not None and token!=row['lease_token']:
                raise ValueError('Lease token mismatch')
            db.execute("UPDATE events SET status='sent',updated=?,provider_id=?,lease_token=NULL WHERE id=?",(time.time(),message_id,event_id))

    def stats(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,stage,status,attempts,provider_id FROM events ORDER BY created')]

    def handoff(self):
        """Structured SKU evidence, independent of whether Gmail has sent the message yet."""
        with self.connect() as db:
            row=db.execute("SELECT id,evidence FROM events WHERE stage='xs_available'").fetchone()
        if row is None or not row['evidence']:
            return None
        observation=json.loads(row['evidence'])
        xs=observation.get('xs',[])
        if len(xs)!=1 or not exact_color(xs[0].get('color',{})) \
                or xs[0].get('au_size')!='6-8' or xs[0].get('matches_requested_us_size') is not True \
                or xs[0].get('is_available') is not True or not re.fullmatch(r'\d+',str(xs[0].get('sku',''))):
            raise ValueError('Stored target evidence is not an exact XS match')
        return {'event_id':row['id'],'sku':str(xs[0]['sku']),'product_id':PRODUCT_ID,
                'product_name':PRODUCT_NAME,'color':xs[0]['color']['name'],'color_id':str(xs[0]['color']['id']),
                'color_group':xs[0]['color'].get('colorGroup'),'us_size':'XS','quantity':1,
                'confirmed_at':observation.get('checked_at_utc'),'stop_at':'payment',
                'requires_fresh_inventory_check':True}


def save_status(stage, **details):
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = {'stage':stage,'updated_at':utcnow().isoformat(),'start':START.isoformat(),'end':END.isoformat(),
            'recipients':list(RECIPIENTS),'interval_seconds':scheduled_interval(),
            'normal_interval_seconds':INTERVAL,'fast_interval_seconds':FAST_INTERVAL,
            'max_in_flight_rounds':MAX_IN_FLIGHT,
            'fast_windows':[[a.isoformat(),b.isoformat()] for a,b in FAST_WINDOWS], **details}
    temp=DATA / f'status-{os.getpid()}.tmp'
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    temp.chmod(0o600)
    temp.replace(DATA/'status.json')
    return data


@contextlib.contextmanager
def monitor_lock():
    DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (DATA/'watch.lock').open('w') as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def confirm_event(first, second):
    a,b=classify(first),classify(second)
    if a['stage'] not in ('listed','xs_available') or b['stage'] not in ('listed','xs_available'):
        return None
    ids_a={str(r['sku']) for r in a['rows']}
    ids_b={str(r['sku']) for r in b['rows']}
    if not ids_a.intersection(ids_b):
        return None
    # A listing can be confirmed while stock is unknown, never invent XS availability.
    if a['stage']!='xs_available' or b['stage']!='xs_available':
        b={**b,'stage':'listed'}
    elif str(a['xs'][0]['sku'])!=str(b['xs'][0]['sku']):
        return None
    return b


def is_new_event(result, stages):
    stage = classify(result)['stage']
    return (stage == 'xs_available' and 'xs_available' not in stages) or (
        stage == 'listed' and not {'listed', 'xs_available'}.intersection(stages))


def run_window(outbox, *, end=END, check_fn=None, interval_fn=scheduled_interval,
               persist_fn=None, status_fn=save_status, max_in_flight=MAX_IN_FLIGHT,
               paused_fn=lambda: False):
    """Bounded pipeline. One coordinator handles files, out-of-order results, and events.

    A positive result pauses launches, drains existing work, then starts a fresh
    confirmation. In-flight responses can never erase a rate-limit backoff.
    """
    check_fn = check_fn or (lambda: check(RAW, us_size='XS', persist=False))
    persist_fn = persist_fn or (lambda result, latest: persist_result(result, RAW, update_latest=latest))
    pending = {}
    issued = checks = latest_seq = failures = failure_batch_through = 0
    next_launch = blocked_until = 0.0
    last_status = 0.0
    positive = None
    last_result = {}
    previous_interval = None
    reached = 'xs_available' in outbox.stages()
    paused = False
    with futures.ThreadPoolExecutor(max_workers=max_in_flight) as pool:
        while utcnow() < end or pending:
            paused = paused or paused_fn()
            done = sorted((job for job in pending if job.done()), key=lambda job: pending[job][0])
            for job in done:
                seq, confirmation_of = pending.pop(job)
                try:
                    result = job.result()
                except Exception as error:
                    result = {'status':'query_failed','has_request_error':True,'requests':[],
                              'checked_at_utc':utcnow().isoformat(),'error':type(error).__name__}
                result['poll_sequence'] = seq
                checks += 1
                newest = seq > latest_seq
                persist_fn(result, newest)
                failed = result.get('has_request_error') or result.get('status') == 'query_failed'
                if failed:
                    if seq > failure_batch_through:
                        failures += 1
                        failure_batch_through = issued
                    blocked_until = max(blocked_until, time.monotonic() + backoff(result, failures))
                    positive = None
                    status_fn('query_failed',pid=os.getpid(),checks=checks,consecutive_failures=failures,
                              next_check_delay_seconds=max(0,blocked_until-time.monotonic()))
                if not newest:
                    continue  # Log old responses, but never use them to restore old stock.
                latest_seq, last_result = seq, result
                if failed or paused or paused_fn() or utcnow() >= end or time.monotonic() < blocked_until:
                    continue
                if seq > failure_batch_through:
                    failures = 0
                if confirmation_of is not None:
                    positive = None
                    observation = confirm_event(confirmation_of, result)
                    if observation and is_new_event(result, outbox.stages()):
                        stages = outbox.stages()
                        if not (observation['stage']=='listed' and {'listed','xs_available'}.intersection(stages)):
                            event = outbox.enqueue(observation)
                            if event:
                                print(json.dumps({'event_id':event,'stage':observation['stage']},ensure_ascii=False),flush=True)
                        reached = 'xs_available' in outbox.stages()
                else:
                    positive = result if is_new_event(result, outbox.stages()) else None

            current = utcnow()
            clock = time.monotonic()
            paused = paused or paused_fn()
            if current >= end or reached or paused:
                if not pending:
                    break
                futures.wait(pending, timeout=1, return_when=futures.FIRST_COMPLETED)
                continue
            interval = interval_fn(current)
            if previous_interval is not None and interval != previous_interval:
                next_launch = clock if interval < previous_interval else max(next_launch,clock+interval)
            previous_interval = interval
            confirming = any(first is not None for _, first in pending.values())
            limit = max_in_flight if interval < 1 else 1
            if clock >= blocked_until and not confirming:
                if positive is not None and not pending:
                    issued += 1
                    pending[pool.submit(check_fn)] = (issued, positive)
                    positive = None
                    next_launch = clock + interval
                elif positive is None and clock >= next_launch and len(pending) < limit:
                    issued += 1
                    pending[pool.submit(check_fn)] = (issued, None)
                    next_launch = clock + interval  # Skip missed slots; never launch a catch-up burst.
            if clock-last_status >= 1:
                status_fn('query_failed' if clock < blocked_until else 'monitoring',pid=os.getpid(),
                          checks=checks,in_flight_rounds=len(pending),consecutive_failures=failures,
                          catalog_status=last_result.get('status'),last_check_at=last_result.get('checked_at_utc'),
                          next_check_delay_seconds=max(0,max(next_launch,blocked_until)-clock))
                last_status = clock
            remaining = max(0,(end-utcnow()).total_seconds())
            timeout = min(remaining,1.0)
            if clock < blocked_until:
                timeout = min(timeout,blocked_until-clock)
            elif positive is None and not confirming and len(pending) < limit:
                timeout = min(timeout,max(0,next_launch-clock))
            if pending:
                futures.wait(pending,timeout=timeout,return_when=futures.FIRST_COMPLETED)
            elif timeout:
                time.sleep(timeout)
    return status_fn('paused_by_user' if paused else 'target_available' if reached else 'window_finished',
                     pid=os.getpid(),checks=checks,running=False,events=outbox.stats())


def watch():
    with monitor_lock():
        if is_paused():
            return save_status('paused_by_user',running=False)
        outbox=Outbox()
        save_status('waiting',pid=os.getpid())
        while utcnow()<START:
            if is_paused():
                return save_status('paused_by_user',running=False)
            time.sleep(max(0,min(1,(START-utcnow()).total_seconds())))
        run_window(outbox,paused_fn=is_paused)


def start_daemon():
    DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
    if is_paused():
        return {'started':False,'reason':'paused_by_user'}
    if utcnow()>=END:
        return {'started':False,'reason':'window_finished'}
    if 'xs_available' in Outbox().stages():
        return {'started':False,'reason':'target_already_confirmed'}
    try:
        with monitor_lock():
            pass
    except BlockingIOError:
        return {'started':False,'reason':'already_running'}
    command=[str(ROOT/'.venv/bin/python'),str(Path(__file__).resolve()),'watch']
    if sys.platform=='darwin':
        command=['/usr/bin/caffeinate','-i',*command]
    with (DATA/'daemon.log').open('ab') as log:
        proc=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                              cwd=ROOT,start_new_session=True)
    (DATA/'launcher-pid').write_text(str(proc.pid))
    return {'started':True,'launcher_pid':proc.pid,'start':START.isoformat(),'end':END.isoformat()}


def control_probe():
    began=time.monotonic()
    result=check(RAW,us_size='XS')
    requests=result['requests']
    product_request=next(r for r in requests if r['label']=='product')
    inventory_request=next(r for r in requests if r['label']=='inventory')
    if any(r.get('error') for r in requests):
        raise RuntimeError('Control API request failed')
    product=json.loads(Path(product_request['body_file']).read_text())
    inventory=json.loads(Path(inventory_request['body_file']).read_text())
    if product['data']['id']!=PRODUCT_ID or product['data']['attributes']['name']!=PRODUCT_NAME:
        raise RuntimeError('Control product mismatch')
    attrs=product['data']['attributes']
    keys={str(s['key']) for s in attrs['sizes'] if any(x.get('region')=='US' and x.get('label')=='XS' for x in s.get('sizeSystems',[]))}
    controls=[s for s in attrs['skus'] if str(s.get('sizeTokenKey')) in keys and s.get('size')=='6-8'
              and s.get('color',{}).get('name')=='Black' and str(s.get('color',{}).get('id')).lstrip('0')=='1']
    if len(controls)!=1:
        raise RuntimeError('Expected unique Black US XS control')
    sku=controls[0]
    stock=next((s['attributes'] for s in inventory['data'] if str(s['id'])==str(sku['id'])),None)
    if stock is None:
        raise RuntimeError('Control inventory record missing')
    return {'control_product':PRODUCT_NAME,'control_color':'Black','control_size':'US XS (AU 6-8)',
            'control_sku':str(sku['id']),'is_available':stock.get('isAvailable'),
            'http_statuses':[r['status_code'] for r in requests],
            'api_round_trip_seconds':round(time.monotonic()-began,3),
            'target_observation':classify(result)['stage'],'checked_at_beijing':utcnow().astimezone(BEIJING).isoformat(timespec='seconds')}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','watch','status','pending','claim','sent','probe','queue-test','handoff'])
    parser.add_argument('--wait-seconds',type=int,default=0)
    parser.add_argument('--event-id')
    parser.add_argument('--message-id')
    parser.add_argument('--lease-token')
    args=parser.parse_args()
    if not 0<=args.wait_seconds<=45:
        parser.error('wait-seconds must be 0..45')
    outbox=Outbox()
    if args.action=='start':
        result=start_daemon()
    elif args.action=='watch':
        watch()
        return 0
    elif args.action=='status':
        p=DATA/'status.json'
        result={'monitor':json.loads(p.read_text()) if p.exists() else None,'events':outbox.stats()}
    elif args.action=='pending':
        deadline=time.monotonic()+args.wait_seconds
        while True:
            events=outbox.pending()
            if events or time.monotonic()>=deadline:
                result=events
                break
            time.sleep(min(0.5,max(0,deadline-time.monotonic())))
    elif args.action=='claim':
        if is_paused():
            raise RuntimeError('Paused by user; email claims are disabled')
        result=outbox.claim(args.event_id)
    elif args.action=='sent':
        outbox.sent(args.event_id,args.message_id,args.lease_token)
        result={'recorded_sent':args.event_id}
    elif args.action=='probe':
        result=control_probe()
    elif args.action=='handoff':
        result=outbox.handoff()
    else:
        control=control_probe()
        event=outbox.enqueue({'stage':'test'},control=control)
        result={'event_id':event,'control':control}
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
