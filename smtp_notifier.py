#!/usr/bin/env python3
"""Direct TLS SMTP delivery. Task-specific credentials stay in macOS Keychain.

Only real durable target events are sent. Setup authenticates but never sends mail.
Each recipient has an independent journal; ambiguous DATA outcomes are not retried.
"""
import argparse
import ctypes
import datetime as dt
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
import fcntl
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import subprocess
import sys
import time

from availability_notifier import DATA, ROOT, START, END, RECIPIENTS, Outbox, utcnow
from monitor_control import is_paused

CONFIG = DATA / 'smtp-config.json'
STATUS = DATA / 'smtp-status.json'
SERVICE = 'qiang.lululemon.availability.smtp'
STOP = END + dt.timedelta(minutes=10)
SERVERS = {'163.com':'smtp.163.com', 'qq.com':'smtp.qq.com', 'gmail.com':'smtp.gmail.com',
           '126.com':'smtp.126.com'}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def keychain(account, password=None):
    """Use native Keychain APIs: no secret command arguments, shell, files or logs."""
    security = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
    core = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    core.CFRelease.argtypes = [ctypes.c_void_p]
    service, user = SERVICE.encode(), account.encode()
    length, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
    find = security.SecKeychainFindGenericPassword
    find.argtypes = [ctypes.c_void_p,ctypes.c_uint32,ctypes.c_char_p,ctypes.c_uint32,
                    ctypes.c_char_p,ctypes.POINTER(ctypes.c_uint32),
                    ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(ctypes.c_void_p)]
    find.restype = ctypes.c_int32
    status = find(None,len(service),service,len(user),user,ctypes.byref(length),ctypes.byref(data),ctypes.byref(item))
    try:
        if password is None:
            if status != 0: raise RuntimeError(f'Task Keychain credential unavailable ({status})')
            return ctypes.string_at(data,length.value).decode()
        raw = password.encode()
        if status == -25300:  # errSecItemNotFound
            add = security.SecKeychainAddGenericPassword
            add.argtypes = [ctypes.c_void_p,ctypes.c_uint32,ctypes.c_char_p,ctypes.c_uint32,
                            ctypes.c_char_p,ctypes.c_uint32,ctypes.c_char_p,ctypes.c_void_p]
            status = add(None,len(service),service,len(user),user,len(raw),raw,None)
        elif status == 0:
            update = security.SecKeychainItemModifyAttributesAndData
            update.argtypes = [ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_char_p]
            status = update(item,None,len(raw),raw)
        if status != 0: raise RuntimeError(f'Task Keychain save failed ({status})')
    finally:
        if data.value:
            security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p,ctypes.c_void_p]
            security.SecKeychainItemFreeContent(None,data)
        if item.value: core.CFRelease(item)


def dialog(prompt, default='', hidden=False):
    script = ('on run argv\nset answer to display dialog (item 1 of argv) with title '
              '"lululemon 邮件通知配置" default answer (item 2 of argv) '
              'buttons {"取消", "继续"} default button "继续" cancel button "取消" '
              + ('with hidden answer\n' if hidden else '\n')
              + 'return text returned of answer\nend run')
    result = subprocess.run(['osascript','-e',script,prompt,default],capture_output=True,text=True)
    if result.returncode: raise RuntimeError('Setup cancelled')
    return result.stdout.rstrip('\n')


def setup():
    account = dialog('填写用于发信的邮箱。支持 163、QQ、Gmail、126；收件人仍为已指定的两个邮箱。',RECIPIENTS[1]).strip()
    if not re.fullmatch(r'[A-Za-z0-9_.+\-]+@(163\.com|qq\.com|gmail\.com|126\.com)',account):
        raise ValueError('Unsupported sender address')
    host = SERVERS[account.rsplit('@',1)[1]]
    password = dialog('填写该邮箱的 SMTP 授权码／应用专用密码，不是邮箱登录密码。\n'
                      '163 可在网页版邮箱 设置 → POP/SMTP/IMAP 开启服务并生成授权码。\n'
                      '仅在本机验证并存入钥匙串；不会发送测试邮件。',hidden=True).strip()
    return configure(account,password)


def configure(account,password):
    if not re.fullmatch(r'[A-Za-z0-9_.+\-]+@(163\.com|qq\.com|gmail\.com|126\.com)',account):
        raise ValueError('Unsupported sender address')
    host=SERVERS[account.rsplit('@',1)[1]]
    password=''.join(password.split()) if account.endswith('@gmail.com') else password.strip()
    if not password or len(password)>256: raise ValueError('Invalid authorization code')
    with smtplib.SMTP_SSL(host,465,context=ssl.create_default_context(),timeout=15) as client:
        client.login(account,password)
    keychain(account,password)
    # Validate that unattended reads work now, without echoing the credential.
    if keychain(account)!=password: raise RuntimeError('Keychain read-back mismatch')
    del password
    result = {'transport':'smtp','host':host,'port':465,'account':account,'keychain_service':SERVICE,
              'authenticated_at_utc':utcnow().isoformat(),'recipients':list(RECIPIENTS)}
    write_json(CONFIG,result)
    write_json(STATUS,{'stage':'configured','running':False,'authenticated':True,'test_mail_sent':False})
    return {'configured':True,'sender':account,'recipients':list(RECIPIENTS),'test_mail_sent':False}


def configuration():
    if not CONFIG.exists(): return None
    config = json.loads(CONFIG.read_text())
    if config.get('host') != SERVERS.get(config.get('account','').rsplit('@',1)[-1]) \
            or config.get('port') != 465 or config.get('keychain_service') != SERVICE \
            or config.get('recipients') != list(RECIPIENTS):
        raise ValueError('Invalid SMTP configuration')
    return config


def build_message(mail, event_id, sender, recipient):
    if mail['to'] != ', '.join(RECIPIENTS) or recipient not in RECIPIENTS:
        raise ValueError('Unapproved email recipient')
    if not re.fullmatch(r'LULU-[a-f0-9]{16}',event_id): raise ValueError('Invalid event')
    message = EmailMessage(policy=SMTP)
    message['Subject'], message['From'], message['To'] = mail['subject'], sender, recipient
    message['Date'] = format_datetime(utcnow())
    message['Message-ID'] = f'<{event_id}.{RECIPIENTS.index(recipient)}@{sender.rsplit("@",1)[1]}>'
    message.set_content('lululemon 澳洲站目标商品状态提醒。请查看 HTML 正文中的准确商品、尺码和官网链接。')
    message.add_alternative(mail['payload']['body']['content'],subtype='html')
    return message


def send_test():
    """One authorized QQ-only test; never touches the production outbox."""
    permission_path=DATA/'mail-permissions.json'
    permission=json.loads(permission_path.read_text()) if permission_path.exists() else {}
    recipient='alerts@example.com'
    if permission.get('allow_test_mail') is not True or permission.get('test_recipients')!=[recipient]:
        raise RuntimeError('QQ-only test email is not authorized')
    config=configuration()
    if not config: raise RuntimeError('SMTP is not configured')
    path=DATA/'qq-smtp-test.json'
    if path.exists(): raise RuntimeError('Test already attempted; inspect saved result before any resend')
    event_id='LULU-QQ-SMTP-'+utcnow().strftime('%Y%m%d-%H%M%S')
    message=EmailMessage(policy=SMTP)
    message['Subject']=f'[{event_id}] 脚本直发测速／非商品上架通知'
    message['From']=config['account']; message['To']=recipient
    message['Date']=format_datetime(utcnow())
    message['Message-ID']=f'<{event_id}@qq.com>'
    message.set_content('这是 QQ SMTP 脚本直发测试，不代表 Classic Lavender Rock 上架。'
                        '仅发送至 QQ，未向 163 发送，未操作购物袋或付款。\n'
                        f'脚本开始时间（UTC）：{utcnow().isoformat()}')
    record={'event_id':event_id,'recipient':recipient,'stage':'connecting',
            'started_at_utc':utcnow().isoformat(),'message_id':str(message['Message-ID'])}
    write_json(path,record)
    started=time.monotonic(); phase='connection'
    try:
        with smtplib.SMTP_SSL(config['host'],465,context=ssl.create_default_context(),timeout=15) as client:
            client.login(config['account'],keychain(config['account']))
            record['connect_auth_seconds']=round(time.monotonic()-started,3)
            phase='send'; record['stage']='sending'; write_json(path,record)
            sent_at=time.monotonic()
            refused=client.send_message(message,from_addr=config['account'],to_addrs=[recipient])
            if refused: raise smtplib.SMTPRecipientsRefused(refused)
            record.update(stage='accepted',smtp_send_seconds=round(time.monotonic()-sent_at,3),
                          total_seconds=round(time.monotonic()-started,3),
                          accepted_at_utc=utcnow().isoformat(),receipt_confirmed=False)
            write_json(path,record)
    except Exception as error:
        if record['stage']!='accepted':
            record.update(stage='unknown' if phase=='send' else 'failed_before_send',
                          error_type=type(error).__name__)
            write_json(path,record)
    return record


class Mailer:
    def __init__(self, box, config, *, connect=None, paused_fn=is_paused):
        self.box, self.config, self.paused_fn = box, config, paused_fn
        self.connect_fn = connect or self.connect
        self.client = None
        self.last_contact = 0
        with box.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS smtp_deliveries ('
                       'event_id TEXT, recipient TEXT, status TEXT, attempts INTEGER DEFAULT 0, '
                       'next_attempt REAL DEFAULT 0, updated REAL, message_id TEXT, error TEXT, '
                       'PRIMARY KEY(event_id,recipient))')
            # A crash during DATA may have accepted the email. Do not send it twice.
            db.execute("UPDATE smtp_deliveries SET status='unknown',error='interrupted_send' WHERE status='sending'")

    def connect(self):
        config = self.config
        client = smtplib.SMTP_SSL(config['host'],465,context=ssl.create_default_context(),timeout=12)
        try: client.login(config['account'],keychain(config['account']))
        except BaseException:
            client.close(); raise
        return client

    def warm(self):
        if self.paused_fn(): return
        if self.client is None: self.client = self.connect_fn()
        elif time.monotonic()-self.last_contact>=30:
            if self.client.noop()[0]!=250: raise ConnectionError('SMTP NOOP failed')
        self.last_contact = time.monotonic()

    def close(self):
        if self.client:
            try: self.client.close()
            except Exception: pass
        self.client = None

    def deliver_pending(self):
        if self.paused_fn(): return []
        with self.box.connect() as db:
            events = db.execute("SELECT id,mail FROM events WHERE stage IN ('listed','xs_available') AND status!='sent'").fetchall()
            for event in events:
                for recipient in RECIPIENTS:
                    db.execute("INSERT OR IGNORE INTO smtp_deliveries(event_id,recipient,status,updated) VALUES (?,?,'pending',?)",
                               (event['id'],recipient,time.time()))
            jobs = db.execute("SELECT d.*,e.mail FROM smtp_deliveries d JOIN events e ON e.id=d.event_id "
                              "WHERE d.status IN ('pending','retry') AND d.next_attempt<=? ORDER BY e.created,d.recipient",(time.time(),)).fetchall()
        results=[]
        for row in jobs:
            if self.paused_fn() or utcnow()>=STOP: break
            message=build_message(json.loads(row['mail']),row['event_id'],self.config['account'],row['recipient'])
            # Connection/authentication errors happen before DATA and can be retried safely.
            phase='connection'; state='retry'; error=None
            try:
                self.warm()
                if self.paused_fn(): break
                with self.box.connect() as db:
                    db.execute("UPDATE smtp_deliveries SET status='sending',attempts=attempts+1,updated=? WHERE event_id=? AND recipient=?",
                               (time.time(),row['event_id'],row['recipient']))
                phase='send'
                refused=self.client.send_message(message,from_addr=self.config['account'],to_addrs=[row['recipient']])
                if refused: raise smtplib.SMTPRecipientsRefused(refused)
                state='accepted'
                self.last_contact=time.monotonic()
            except smtplib.SMTPRecipientsRefused as exc:
                code=exc.recipients.get(row['recipient'],(550,b''))[0]
                state='retry' if 400<=code<500 else 'failed'; error=f'SMTP_{code}'
            except smtplib.SMTPResponseException as exc:
                state='retry' if 400<=exc.smtp_code<500 else 'failed'; error=f'SMTP_{exc.smtp_code}'
                self.close()
            except Exception as exc:
                state='unknown' if phase=='send' else 'retry'; error=type(exc).__name__
                self.close()
            attempts=row['attempts']+1
            if state=='retry' and attempts>=4: state='failed'
            with self.box.connect() as db:
                db.execute('UPDATE smtp_deliveries SET status=?,attempts=?,next_attempt=?,updated=?,message_id=?,error=? '
                           'WHERE event_id=? AND recipient=?',
                           (state,attempts,time.time()+min(5*3**(attempts-1),120),time.time(),str(message['Message-ID']),error,row['event_id'],row['recipient']))
                states=[r[0] for r in db.execute('SELECT status FROM smtp_deliveries WHERE event_id=?',(row['event_id'],))]
            if len(states)==len(RECIPIENTS) and all(s=='accepted' for s in states):
                self.box.sent(row['event_id'],'smtp:'+row['event_id'])
            results.append({'event_id':row['event_id'],'recipient':row['recipient'],'status':state,'error':error})
        return results


def status():
    with Outbox().connect() as db:
        exists=db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='smtp_deliveries'").fetchone()
        deliveries=[dict(row) for row in db.execute('SELECT * FROM smtp_deliveries')] if exists else []
    return {'configured':CONFIG.exists(),'worker':json.loads(STATUS.read_text()) if STATUS.exists() else None,
            'deliveries':deliveries}


def watch():
    DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (DATA/'smtp.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        config=configuration()
        if not config: raise RuntimeError('SMTP is not configured')
        mailer=Mailer(Outbox(),config)
        stage='waiting'; last_status=0; next_warm=0
        try:
            while utcnow()<STOP and not is_paused():
                now=utcnow()
                if now>=START-dt.timedelta(seconds=30) and time.monotonic()>=next_warm:
                    try:
                        mailer.warm(); stage='ready'
                    except Exception as error:
                        mailer.close(); stage='connection_unavailable'
                    next_warm=time.monotonic()+30
                if now>=START:
                    for item in mailer.deliver_pending(): print(json.dumps(item),flush=True)
                if time.monotonic()-last_status>=5:
                    write_json(STATUS,{'stage':stage,'running':True,'pid':os.getpid(),
                                       'updated_at_utc':now.isoformat(),'end':STOP.isoformat()})
                    last_status=time.monotonic()
                time.sleep(0.05 if now>=START else 1)
        finally:
            mailer.close()
            write_json(STATUS,{'stage':'paused_by_user' if is_paused() else 'window_finished','running':False,'pid':os.getpid()})


def start():
    if is_paused(): return {'started':False,'reason':'paused_by_user'}
    if utcnow()>=STOP: return {'started':False,'reason':'window_finished'}
    if not configuration(): return {'started':False,'reason':'smtp_not_configured'}
    with (DATA/'smtp.lock').open('w') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return {'started':False,'reason':'already_running'}
    command=['/usr/bin/caffeinate','-i',str(ROOT/'.venv/bin/python'),str(Path(__file__).resolve()),'watch']
    with (DATA/'smtp.log').open('ab') as log:
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=log,cwd=ROOT,start_new_session=True)
    return {'started':True,'launcher_pid':process.pid}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['setup','start','watch','status','test'])
    args=parser.parse_args()
    try:
        output=send_test() if args.action=='test' else globals()[args.action]()
        if output is not None: print(json.dumps(output,ensure_ascii=False))
    except Exception as error:
        # SMTP exceptions can include server text: emit only the class and numeric code.
        print(json.dumps({'ok':False,'error_type':type(error).__name__,'smtp_code':getattr(error,'smtp_code',None)}))
        raise SystemExit(1)
