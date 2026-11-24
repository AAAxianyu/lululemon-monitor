"""Finish already-verified shipping in ordinary Chrome; stop on Payment."""
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path
from checkout_browser import ROOT
from task_browser import launch_normal_shipping
from monitor_control import is_paused

HELPER=ROOT/'.private/native_checkout_button'
NAME='Scuba Evolve Classic Full-Zip Hoodie'

def verify(state,color,*,payment=False):
    text=[str(x).strip() for x in state.get('texts',[]) if str(x).strip()]
    url=state.get('url','').removeprefix('https://').removeprefix('www.')
    expected='lululemon.com.au/en-au/shop/checkout/'+('payment' if payment else 'shipping')
    if url!=expected:raise ValueError('Wrong checkout step')
    import re as _re
    if not (color=='Black' or _re.fullmatch(r'Lavender Rock(\s*/\s*[^/]+)?',color)) or text.count(NAME)!=1 or text.count(color)!=1:
        raise ValueError('Wrong merchandise')
    if 'US XS (AU 6-8)' not in text or '1 item' not in text or 'Qty' not in text or text[text.index('Qty')+1]!='1':
        raise ValueError('Wrong size or quantity')
    if 'Final Sale' in ' '.join(text):raise ValueError('Final sale')
    for required in ('recipient2@example.com','Jane','Doe','+61 400000000','1 Example Street',
                     'Example Post Office Parcel Locker','Sydney','NSW','2000','Australia'):
        if required not in text:raise ValueError('Address/contact mismatch')
    if not payment:
        selected=[r for r in state.get('radios',[]) if r['selected'] and ' | lululemon' not in r['label']]  # browser tab strip also reports as a radio
        if len(selected)!=1 or not all(x in selected[0]['label'] for x in ('Jane','Doe','1 Example Street','2020')):
            raise ValueError('Selected address mismatch')
        leave=[c for c in state.get('checkboxes',[]) if c['label']=='Authority to leave package without signature']
        if len(leave)!=1 or leave[0]['checked']:raise ValueError('Leave-without-signature mismatch')
    return True

def finish(color,deadline):
    def guard():
        if is_paused() or time.time()>=deadline:raise RuntimeError('Paused or outside execution window')
    last_error=None
    guard();pid=launch_normal_shipping();started=time.monotonic()
    def inspect():return json.loads(subprocess.check_output([str(HELPER),str(pid),'inspect'],text=True,timeout=10))
    while time.monotonic()-started<150:
        guard();state=inspect()
        if state.get('url','').endswith('/checkout/shipping') and 'Continue' in state['buttons']:
            try: verify(state,color)
            except ValueError as e:  # tree may still be partially rendered; keep polling until timeout
                last_error=e; time.sleep(.25); continue
            ready_at=round(time.monotonic()-started,3);break
        time.sleep(.25)
    else:raise (last_error if last_error else RuntimeError('Shipping did not become ready'))
    guard()
    subprocess.run([str(HELPER),str(pid),'Continue'],check=True,capture_output=True,text=True,timeout=10)
    while time.monotonic()-started<200:
        guard();state=inspect()
        if state.get('url','').endswith('/checkout/payment'):
            try: verify(state,color,payment=True)
            except ValueError as e:
                last_error=e; time.sleep(.25); continue
            screenshot=ROOT/'checkout_data/rehearsal'/('native-'+color.lower().replace(' ','-')+'-payment.png')
            if not state.get('main_window_id'):raise RuntimeError('Screenshot window missing')
            subprocess.run(['screencapture','-x','-l',str(state['main_window_id']),str(screenshot)],check=True)
            return {'stage':'ready_for_manual_card','submitted':False,'native_seconds':round(time.monotonic()-started,3),'shipping_ready_seconds':ready_at,
                    'screenshot':str(screenshot),'browser_pid':pid,'name':NAME,'color':color,'size':'US XS (AU 6-8)','quantity':1}
        if 'RETURN TO CHECKOUT' in state['buttons']:raise RuntimeError('Shipping rejected in ordinary browser')
        time.sleep(.25)
    raise (last_error if last_error else RuntimeError('Payment not reached'))

if __name__=='__main__':
    try:print(json.dumps(finish(sys.argv[1],float(sys.argv[2]))))
    except Exception as e:
        print(json.dumps({'stage':'native_shipping_stopped','error_type':type(e).__name__,'message':str(e)[:200],'submitted':False}));sys.exit(1)
