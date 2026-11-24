"""Local event -> standalone browser process. No Codex queue or model calls."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from availability_notifier import DATA, ROOT, START, END, Outbox, utcnow
from event_dispatcher import write_json
from monitor_control import is_paused

DIRECT=DATA/'direct-browser'

def handle(handoff, *, run=subprocess.run):
    if is_paused(): return {'stage':'paused_by_user'}
    if not START<=utcnow()<END: return {'stage':'outside_window'}
    if (handoff.get('product_id')!='lopmi7nqah' or not __import__('re').fullmatch(r'Lavender Rock(\s*/\s*[^/]+)?',str(handoff.get('color','')))
        or handoff.get('us_size')!='XS' or handoff.get('quantity')!=1
        or not str(handoff.get('sku','')).isdigit()):
        raise ValueError('Invalid exact-target handoff')
    DIRECT.mkdir(parents=True,exist_ok=True,mode=0o700)
    claim=DIRECT/('attempt-'+handoff['sku']+'.json')
    try: fd=os.open(claim,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError: return {'stage':'already_attempted'}
    with os.fdopen(fd,'w') as file:
        json.dump({'sku':handoff['sku'],'started_at':utcnow().isoformat()},file)
        file.flush(); os.fsync(file.fileno())
    try:
        result=run([shutil.which('node'),'direct_checkout.mjs','production',handoff['sku']],
                   cwd=ROOT,capture_output=True,text=True,timeout=360)
        # Only our structured status is exposed; browser errors never include form values.
        outcome=json.loads(result.stdout.strip().splitlines()[-1])
    except Exception as error: outcome={'stage':'execution_unknown','error_type':type(error).__name__}
    write_json(DIRECT/'status.json',outcome)
    return outcome

def watch():
    DIRECT.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (DIRECT/'worker.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        while utcnow()<END and not is_paused():
            ready_path=DATA/'direct-browser-ready.json'
            ready=ready_path.exists() and json.loads(ready_path.read_text()).get('ready') is True
            if utcnow()<START or not ready:
                write_json(DIRECT/'status.json',{'stage':'waiting' if ready else 'setup_required',
                    'running':True,'pid':os.getpid()})
                time.sleep(1); continue
            handoff=Outbox().handoff()
            if handoff:
                print(json.dumps(handle(handoff)),flush=True); return
            time.sleep(.05)

if __name__=='__main__': watch()
