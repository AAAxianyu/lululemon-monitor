import json
import smtplib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import smtp_notifier

from availability_notifier import Outbox, classify, RECIPIENTS
from test_availability_notifier import result
from smtp_notifier import Mailer, build_message
from monitor_control import is_paused, set_paused


class Client:
    def __init__(self, failures=None):
        self.messages=[]; self.failures=failures or {}
    def send_message(self,message,**kwargs):
        self.messages.append((message,kwargs))
        recipient=kwargs['to_addrs'][0]
        failure=self.failures.pop(recipient,None)
        if failure: raise failure
        return {}
    def noop(self): return 250,b'ok'
    def close(self): pass


class SmtpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.box=Outbox(self.temp.name)
        self.event=self.box.enqueue(classify(result()))
        self.config={'account':'sender@163.com','host':'smtp.163.com'}

    def deliver(self,client):
        return Mailer(self.box,self.config,connect=lambda:client,paused_fn=lambda:False)

    def test_detection_to_two_smtp_envelopes_and_persistent_dedup(self):
        client=Client(); sender=self.deliver(client)
        rows=sender.deliver_pending()
        self.assertEqual({row['recipient'] for row in rows},set(RECIPIENTS))
        self.assertTrue(all(row['status']=='accepted' for row in rows))
        self.assertEqual(self.box.stats()[0]['status'],'sent')
        self.assertEqual(sender.deliver_pending(),[])
        self.assertEqual(self.deliver(client).deliver_pending(),[])
        self.assertEqual(len(client.messages),len(RECIPIENTS))
        for msg,args in client.messages:
            self.assertIn('Classic Lavender Rock',msg['Subject'])
            self.assertEqual(args['to_addrs'],[str(msg['To'])])
            self.assertIn('Scuba Evolve Classic Full-Zip Hoodie',msg.get_body(('html',)).get_content())

    def test_partial_failure_does_not_resend_successful_recipient(self):
        client=Client({RECIPIENTS[1]:smtplib.SMTPRecipientsRefused({RECIPIENTS[1]:(450,b'temporary')})})
        sender=self.deliver(client); sender.deliver_pending()
        self.assertNotEqual(self.box.stats()[0]['status'],'sent')
        with self.box.connect() as db: db.execute('UPDATE smtp_deliveries SET next_attempt=0')
        sender.deliver_pending()
        self.assertEqual([m[1]['to_addrs'][0] for m in client.messages].count(RECIPIENTS[0]),1)
        self.assertEqual([m[1]['to_addrs'][0] for m in client.messages].count(RECIPIENTS[1]),2)
        self.assertEqual(self.box.stats()[0]['status'],'sent')

    def test_timeout_after_data_is_unknown_not_blind_retry(self):
        client=Client({RECIPIENTS[0]:TimeoutError('ambiguous')})
        sender=self.deliver(client); rows=sender.deliver_pending()
        self.assertIn('unknown',[row['status'] for row in rows])
        with self.box.connect() as db: db.execute('UPDATE smtp_deliveries SET next_attempt=0')
        self.assertEqual(sender.deliver_pending(),[])
        self.assertEqual(len(client.messages),len(RECIPIENTS))
        self.assertNotEqual(self.box.stats()[0]['status'],'sent')

    def test_crash_during_send_does_not_duplicate(self):
        sender=self.deliver(Client())
        with self.box.connect() as db:
            db.execute("INSERT INTO smtp_deliveries(event_id,recipient,status,updated) VALUES (?,?,'sending',?)",
                       (self.event,RECIPIENTS[0],time.time()))
        client=Client(); sender=self.deliver(client); sender.deliver_pending()
        self.assertEqual(len(client.messages),len(RECIPIENTS)-1)
        self.assertEqual(sorted(m[1]['to_addrs'][0] for m in client.messages),sorted(RECIPIENTS[1:]))

    def test_connection_failure_retries_are_bounded(self):
        def connect(): raise ConnectionError('offline')
        sender=Mailer(self.box,self.config,connect=connect,paused_fn=lambda:False)
        for _ in range(4):
            sender.deliver_pending()
            with self.box.connect() as db: db.execute('UPDATE smtp_deliveries SET next_attempt=0')
        self.assertEqual(sender.deliver_pending(),[])
        with self.box.connect() as db:
            self.assertEqual({r['status'] for r in db.execute('SELECT status FROM smtp_deliveries')},{'failed'})

    def test_pause_prevents_send_and_fail_closed_corrupt_marker(self):
        client=Client(); sender=Mailer(self.box,self.config,connect=lambda:client,paused_fn=lambda:True)
        self.assertEqual(sender.deliver_pending(),[])
        self.assertEqual(client.messages,[])
        path=Path(self.temp.name)/'pause.json'
        set_paused(True,reason='test',path=path); self.assertTrue(is_paused(path))
        set_paused(False,reason='test',path=path); self.assertFalse(is_paused(path))
        path.write_text('{broken'); self.assertTrue(is_paused(path))

    def test_test_events_are_not_sent_again(self):
        self.box.sent(self.event,'already-sent')
        self.box.enqueue({'stage':'test'},control={'color':'Black'})
        client=Client(); self.assertEqual(self.deliver(client).deliver_pending(),[])
        self.assertEqual(client.messages,[])

    def test_test_email_envelope_is_qq_only_and_deduplicated(self):
        directory=Path(self.temp.name)
        (directory/'mail-permissions.json').write_text(json.dumps(
            {'allow_test_mail':True,'test_recipients':[RECIPIENTS[0]]}))
        client=MagicMock(); client.send_message.return_value={}
        with patch.object(smtp_notifier,'DATA',directory), \
             patch.object(smtp_notifier,'configuration',return_value=self.config), \
             patch.object(smtp_notifier,'keychain',return_value='test-only'), \
             patch.object(smtp_notifier.smtplib,'SMTP_SSL') as connect:
            connect.return_value.__enter__.return_value=client
            self.assertEqual(smtp_notifier.send_test()['stage'],'accepted')
            self.assertEqual(client.send_message.call_args.kwargs['to_addrs'],[RECIPIENTS[0]])
            with self.assertRaises(RuntimeError): smtp_notifier.send_test()
            self.assertEqual(client.send_message.call_count,1)


if __name__=='__main__': unittest.main()
