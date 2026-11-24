"""Restart only the task's dedicated profile, preserving its authenticated session."""
import json
import os
import signal
import subprocess
import time
from checkout_browser import ROOT, CHROME, selected_profile

def owned_process():
    wanted='--user-data-dir='+str(selected_profile())
    rows=[]
    for line in subprocess.check_output(['ps','-axo','pid=,command='],text=True).splitlines():
        fields=line.strip().split(None,1)
        if len(fields)==2 and fields[1].startswith(str(CHROME)+' ') and wanted in fields[1].split():
            rows.append((int(fields[0]),fields[1]))
    if len(rows)>1: raise RuntimeError('Ambiguous dedicated browser')
    return rows[0] if rows else None

def stop_owned():
    row=owned_process()
    if row is None:return
    os.kill(row[0],signal.SIGTERM)
    for _ in range(80):
        try:os.kill(row[0],0)
        except ProcessLookupError:return
        time.sleep(.1)
    raise RuntimeError('Dedicated browser did not exit; never force kill')

def stop_if_manual():
    row=owned_process()
    if row and '--remote-debugging-port' not in row[1]:stop_owned()

def launch_normal_shipping():
    stop_owned()
    with (ROOT/'.private/manual-chrome-launch.log').open('ab') as log:
        proc=subprocess.Popen([str(CHROME),'--user-data-dir='+str(selected_profile()),
            '--no-first-run','--no-default-browser-check','--force-renderer-accessibility',
            'https://www.lululemon.com.au/en-au/shop/checkout/shipping'],
            stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    return proc.pid
