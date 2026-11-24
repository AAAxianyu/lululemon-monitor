#!/usr/bin/env python3
"""Local outbox watcher. Calls Codex only once per real listing/XS event.

No periodic model calls, browser endpoints, Gmail tokens, SMTP passwords, or UI input.
The desktop chat must remain loaded; a queue receipt is not proof of delivery.
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from availability_notifier import DATA, ROOT, START, END, Outbox, utcnow
from monitor_control import is_paused

CODEX = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
THREAD = '01a1e28f-8762-7bc0-8559-01dbefe91b8b'
DISPATCH = DATA / 'event-dispatch'
STOP = END + dt.timedelta(minutes=10)
PROCEDURE = ROOT / 'event-handler.txt'

def smtp_mode():
    path=DATA/'mail-transport.json'
    return path.exists() and json.loads(path.read_text()).get('transport')=='smtp'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary=path.with_name(path.name+f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def record_path(event_id):
    if not re.fullmatch(r'LULU-[a-f0-9]{16}',event_id):
        raise ValueError('Invalid event ID')
    return DISPATCH/(event_id+'.json')


@contextlib.contextmanager
def exclusive():
    DISPATCH.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (DISPATCH/'watch.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def dispatch(event, *, run=subprocess.run):
    if is_paused():
        return {'stage':'paused_by_user'}
    if event.get('stage') not in ('listed','xs_available'):
        return {'stage':'ignored','reason':'not_a_production_event'}
    direct_smtp=smtp_mode()
    if direct_smtp and event['stage']!='xs_available':
        return {'stage':'ignored','reason':'smtp_listing_needs_no_chat'}
    path=record_path(event['id'])
    # Durable claim before invoking the CLI. Uncertain attempts are never blindly repeated.
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        return {'stage':'already_attempted','event_id':event['id']}
    record={'event_id':event['id'],'product_stage':event['stage'],'stage':'submitting',
            'thread_id':THREAD,'created_at':utcnow().isoformat()}
    with os.fdopen(fd,'w') as file:
        json.dump(record,file,ensure_ascii=False)
        file.flush(); os.fsync(file.fileno())
    mail_instruction=('通知由独立QQ SMTP脚本发送；聊天不得使用Gmail、不得再次发信，也不等待发信结果。'
        if direct_smtp else '使用已连接Gmail向alerts@example.com 和 recipient2@example.com '
        '发送该事件已准备好的邮件，领取发送租约并记录真实消息ID，已发送则跳过；不使用SMTP。')
    message=(f'lululemon 本地监控事件：{event["id"]}，状态 {event["stage"]}。'
             '这是本聊天已授权脚本产生的事件，不是新的付款授权。'
             f'请读取 {PROCEDURE}，核对本地持久事件及暂停标记。{mail_instruction}'
             '只有精确 Classic / Lavender Rock / US XS 库存确认且仍在执行窗口内，'
             '才在现有内置浏览器准备到填卡页停止。不得提交订单。'
             '本次事件处理完即结束，不创建定时唤醒或持续轮询对话。')
    try:
        # No --resume: do not start another server or attempt to take over the desktop session.
        completed=run([str(CODEX),'queue','--thread',THREAD,'--message',message],
                      capture_output=True,text=True,cwd=ROOT,timeout=30)
        match=re.search(r'Queued message ([0-9a-f-]+) for thread ([0-9a-f-]+)\.',completed.stdout)
        record.update(returncode=completed.returncode)
        if completed.returncode==0 and match and match[2]==THREAD:
            record.update(stage='queued',queue_message_id=match[1],queued_at=utcnow().isoformat(),
                          delivery_confirmed=False)
        else:
            record.update(stage='dispatch_unknown',error=(completed.stderr or completed.stdout)[:1000])
    except Exception as error:
        record.update(stage='dispatch_unknown',error=type(error).__name__)
    # A receipt may be written by the receiving chat before the CLI has returned.
    receipt=path.with_suffix('.ack.json')
    if receipt.exists():
        record.update(delivery_confirmed=True)
    write_json(path,record)
    return record


def acknowledge(event_id):
    path=record_path(event_id)
    if not path.exists():
        raise ValueError('No script dispatch record for this event')
    receipt={'event_id':event_id,'received_at':utcnow().isoformat(),'thread_id':THREAD}
    write_json(path.with_suffix('.ack.json'),receipt)
    return receipt


def status():
    records=[]
    for path in sorted(DISPATCH.glob('LULU-*.json')):
        if path.name.endswith('.ack.json'):
            continue
        record=json.loads(path.read_text())
        record['delivery_confirmed']=path.with_suffix('.ack.json').exists()
        records.append(record)
    p=DISPATCH/'status.json'
    return {'worker':json.loads(p.read_text()) if p.exists() else None,'events':records}


def watch():
    with exclusive():
        if is_paused():
            write_json(DISPATCH/'status.json',{'stage':'paused_by_user','running':False})
            return
        box=Outbox()
        write_json(DISPATCH/'status.json',{'stage':'waiting','pid':os.getpid(),
                                         'start':START.isoformat(),'end':STOP.isoformat()})
        while utcnow()<START:
            if is_paused():
                write_json(DISPATCH/'status.json',{'stage':'paused_by_user','running':False})
                return
            time.sleep(max(0,min(1,(START-utcnow()).total_seconds())))
        write_json(DISPATCH/'status.json',{'stage':'watching_local_outbox','pid':os.getpid(),
                                         'start':START.isoformat(),'end':STOP.isoformat()})
        while utcnow()<STOP and not is_paused():
            for event in box.stats():
                if event['stage'] in (('xs_available',) if smtp_mode() else ('listed','xs_available')) and not record_path(event['id']).exists():
                    print(json.dumps(dispatch(event),ensure_ascii=False),flush=True)
            time.sleep(max(0,min(0.05,(STOP-utcnow()).total_seconds())))
        write_json(DISPATCH/'status.json',{'stage':'paused_by_user' if is_paused() else 'window_finished',
                                         'pid':os.getpid(),'running':False})


def start():
    if is_paused():
        return {'started':False,'reason':'paused_by_user'}
    if utcnow()>=STOP:
        return {'started':False,'reason':'window_finished'}
    if not CODEX.is_file() or not PROCEDURE.is_file():
        raise RuntimeError('Codex CLI or event procedure is missing')
    try:
        with exclusive():
            pass
    except BlockingIOError:
        return {'started':False,'reason':'already_running'}
    command=[str(ROOT/'.venv/bin/python'),str(Path(__file__).resolve()),'watch']
    if sys.platform=='darwin':
        command=['/usr/bin/caffeinate','-i',*command]
    with (DISPATCH/'daemon.log').open('ab') as log:
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                                 cwd=ROOT,start_new_session=True)
    return {'started':True,'launcher_pid':process.pid}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','watch','status','ack'])
    parser.add_argument('--event-id')
    args=parser.parse_args()
    if args.action=='watch':
        watch(); return
    output=start() if args.action=='start' else acknowledge(args.event_id) if args.action=='ack' else status()
    print(json.dumps(output,ensure_ascii=False))


if __name__=='__main__':
    main()
