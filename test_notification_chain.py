import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import notification_chain as chain
from availability_notifier import Outbox, run_window, utcnow, RECIPIENTS
from smtp_notifier import Mailer
from test_availability_notifier import result
from test_smtp_notifier import Client


class ChainTests(unittest.TestCase):
    def test_configuration_failure_cannot_clear_user_pause(self):
        with patch.object(chain,'configuration',return_value=None), \
             patch.object(chain,'set_paused') as resume:
            self.assertEqual(chain.start(resume=True)['reason'],'gmail_event_setup_required')
            resume.assert_not_called()

    def test_plain_start_preserves_pause(self):
        with patch.object(chain,'configuration',return_value={'transport':'gmail_connector'}), \
             patch.object(chain,'is_paused',return_value=True), \
             patch.object(chain.subprocess,'Popen') as process:
            self.assertEqual(chain.start()['reason'],'paused_by_user')
            process.assert_not_called()

    def test_gmail_chain_never_starts_smtp_worker(self):
        self.assertEqual(set(chain.WORKERS.values()),{'availability_notifier.py','event_dispatcher.py'})

    def test_smtp_chain_has_independent_mail_worker(self):
        self.assertEqual(set(chain.workers({'transport':'smtp'}).values()),
                         {'availability_notifier.py','smtp_notifier.py','event_dispatcher.py'})

    def test_direct_mode_has_no_chat_dispatcher(self):
        self.assertEqual(set(chain.workers({'transport':'smtp','browser_executor':'direct'}).values()),
                         {'availability_notifier.py','smtp_notifier.py','direct_browser_worker.py'})

    def test_detector_confirmation_feeds_both_smtp_recipients_without_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            box=Outbox(directory)
            sequence=iter([{**result(),'target_skus':[]},result(),result()])
            outcome=run_window(box,end=utcnow()+dt.timedelta(seconds=1),
                check_fn=lambda:next(sequence),interval_fn=lambda _:0.001,max_in_flight=1,
                persist_fn=lambda *_:None,status_fn=lambda stage,**kw:{'stage':stage})
            self.assertEqual(outcome['stage'],'target_available')
            client=Client()
            with patch('subprocess.run',side_effect=AssertionError('No chat or external commands permitted')):
                rows=Mailer(box,{'account':'sender@163.com'},connect=lambda:client,
                            paused_fn=lambda:False).deliver_pending()
            self.assertEqual({r['recipient'] for r in rows},set(RECIPIENTS))
            self.assertTrue(all(r['status']=='accepted' for r in rows))
            self.assertEqual(len(box.stats()),1)
            self.assertEqual(box.stats()[0]['status'],'sent')


if __name__=='__main__': unittest.main()
