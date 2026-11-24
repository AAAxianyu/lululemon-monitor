#!/usr/bin/env python3
"""Bounded local supervisor: HTTP detector + event-only Gmail/IAB handoff.

No recurring model automation. Does not resurrect itself after a user pause.
"""
import argparse
import fcntl
import json
import os
import subprocess
import time

from availability_notifier import DATA, ROOT, START, END, Outbox, utcnow
from monitor_control import is_paused, set_paused
from event_dispatcher import write_json, STOP, CODEX, PROCEDURE

STATE = DATA / 'chain-status.json'
WORKERS = {'detector':'availability_notifier.py','gmail_browser_events':'event_dispatcher.py'}

def workers(config):
    if config['transport']=='smtp':
        return {'detector':'availability_notifier.py','smtp':'smtp_notifier.py',
                'browser_events':'direct_browser_worker.py' if config.get('browser_executor')=='direct' else 'event_dispatcher.py'}
    return WORKERS


def configuration():
    path=DATA/'mail-transport.json'
    if not path.exists(): return None
    value=json.loads(path.read_text())
    if value.get('transport') not in ('gmail_connector','smtp') or not CODEX.is_file() or not PROCEDURE.is_file():
        return None
    if value['transport']=='smtp':
        from smtp_notifier import configuration as smtp_configuration
        if not smtp_configuration(): return None
    return value


def running():
    DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (DATA/'chain.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return True
    return False


def status():
    return {'paused':is_paused(),'supervisor_running':running(),
            'state':json.loads(STATE.read_text()) if STATE.exists() else None}


def watch():
    with (DATA/'chain.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        config=configuration()
        if is_paused() or not config: return
        active_workers=workers(config)
        children={}; records={name:{'launches':0,'next_start':0} for name in active_workers}
        try:
            while utcnow()<STOP and not is_paused():
                target_found='xs_available' in Outbox().stages()
                for name,script in active_workers.items():
                    row=records[name]; process=children.get(name)
                    if process is not None:
                        code=process.poll()
                        if code is None: continue
                        row.update(exit_code=code,running=False)
                        children.pop(name)
                        if code==0:
                            row['completed']=True
                        else:
                            row['next_start']=time.monotonic()+min(5*3**(row['launches']-1),60)
                    if row.get('completed') or time.monotonic()<row['next_start']:
                        continue
                    if name=='detector' and (utcnow()>=END or target_found):
                        row['completed']=True; continue
                    with (DATA/(name+'-worker.log')).open('ab') as output:
                        process=subprocess.Popen([str(ROOT/'.venv/bin/python'),str(ROOT/script),'watch'],
                            stdin=subprocess.DEVNULL,stdout=output,stderr=output,cwd=ROOT)
                    children[name]=process
                    row.update(pid=process.pid,running=True,launches=row['launches']+1)
                write_json(STATE,{'stage':'waiting' if utcnow()<START else 'running',
                                  'pid':os.getpid(),'running':True,'workers':records,'mail_transport':config['transport'],
                                  'start':START.isoformat(),'end':END.isoformat(),
                                  'updated_at_utc':utcnow().isoformat()})
                time.sleep(0.5)
        finally:
            for process in children.values():
                if process.poll() is None: process.terminate()
            for process in children.values():
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            for row in records.values(): row['running']=False
            write_json(STATE,{'stage':'paused_by_user' if is_paused() else 'window_finished',
                              'pid':os.getpid(),'running':False,'workers':records,
                              'updated_at_utc':utcnow().isoformat()})


def start(*, resume=False):
    if utcnow()>=STOP: return {'started':False,'reason':'window_finished'}
    # Never clear a user pause before required configuration is ready.
    if not configuration(): return {'started':False,'reason':'gmail_event_setup_required'}
    if resume:
        set_paused(False,reason='用户授权0.1秒检测、配置的双邮箱通知及自动到填卡页；不周期唤醒聊天')
    if is_paused(): return {'started':False,'reason':'paused_by_user'}
    if running(): return {'started':False,'reason':'already_running'}
    command=['/usr/bin/caffeinate','-i',str(ROOT/'.venv/bin/python'),str(ROOT/'notification_chain.py'),'watch']
    with (DATA/'chain.log').open('ab') as output:
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=output,stderr=output,cwd=ROOT,start_new_session=True)
    return {'started':True,'launcher_pid':process.pid,'start':START.isoformat(),'end':END.isoformat()}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','resume','stop','watch','status'])
    args=parser.parse_args()
    if args.action=='watch': watch()
    else:
        value=(start(resume=args.action=='resume') if args.action in ('start','resume') else
               set_paused(True,reason='用户暂停脚本链') if args.action=='stop' else status())
        print(json.dumps(value,ensure_ascii=False))
