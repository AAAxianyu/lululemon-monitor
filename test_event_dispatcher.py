import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import event_dispatcher as dispatcher


class EventDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.patch=patch.object(dispatcher,'DISPATCH',self.directory)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.pause_patch=patch.object(dispatcher,'is_paused',return_value=False)
        self.pause_patch.start(); self.addCleanup(self.pause_patch.stop)
        self.smtp_patch=patch.object(dispatcher,'smtp_mode',return_value=False)
        self.smtp_patch.start(); self.addCleanup(self.smtp_patch.stop)
        self.event={'id':'LULU-1234567890abcdef','stage':'xs_available'}

    def test_only_real_events_and_one_queue_attempt(self):
        calls=[]
        def run(argv,**kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv,0,
                'Queued message 01234567-abcd-1234-abcd-012345678901 for thread '+dispatcher.THREAD+'.\n','')
        self.assertEqual(dispatcher.dispatch({**self.event,'stage':'test'},run=run)['stage'],'ignored')
        self.assertEqual(dispatcher.dispatch(self.event,run=run)['stage'],'queued')
        self.assertEqual(dispatcher.dispatch(self.event,run=run)['stage'],'already_attempted')
        self.assertEqual(len(calls),1)
        self.assertNotIn('--resume',calls[0])
        self.assertIn('event-handler.txt',calls[0][-1])
        self.assertIn('alerts@example.com 和 recipient2@example.com',calls[0][-1])
        self.assertIn('不使用SMTP',calls[0][-1])

    def test_listing_alone_triggers_gmail_event_once(self):
        def run(argv,**kwargs):
            return subprocess.CompletedProcess(argv,0,
                'Queued message 01234567-abcd-1234-abcd-012345678901 for thread '+dispatcher.THREAD+'.','')
        event={**self.event,'stage':'listed'}
        self.assertEqual(dispatcher.dispatch(event,run=run)['stage'],'queued')
        self.assertEqual(dispatcher.dispatch(event,run=run)['stage'],'already_attempted')

    def test_unknown_queue_outcome_is_not_retried(self):
        def run(*args,**kwargs):
            raise subprocess.TimeoutExpired('codex',30)
        self.assertEqual(dispatcher.dispatch(self.event,run=run)['stage'],'dispatch_unknown')
        self.assertEqual(dispatcher.dispatch(self.event,run=run)['stage'],'already_attempted')

    def test_receipt_is_distinct_from_queue_acceptance_and_email_status(self):
        def run(argv,**kwargs):
            return subprocess.CompletedProcess(argv,0,
                'Queued message 01234567-abcd-1234-abcd-012345678901 for thread '+dispatcher.THREAD+'.','')
        dispatcher.dispatch(self.event,run=run)
        self.assertFalse(dispatcher.status()['events'][0]['delivery_confirmed'])
        dispatcher.acknowledge(self.event['id'])
        self.assertTrue(dispatcher.status()['events'][0]['delivery_confirmed'])
        self.assertNotIn('sent',json.dumps(dispatcher.status()))

    def test_event_id_cannot_escape_private_directory(self):
        with self.assertRaises(ValueError):
            dispatcher.record_path('../../anything')

    def test_paused_event_cannot_wake_chat(self):
        with patch.object(dispatcher,'is_paused',return_value=True):
            self.assertEqual(dispatcher.dispatch(self.event,run=lambda *_a,**_k:self.fail('must not queue'))['stage'],
                             'paused_by_user')

    def test_smtp_listing_no_chat_and_xs_never_requests_gmail(self):
        calls=[]
        def run(argv,**kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv,0,
                'Queued message 01234567-abcd-1234-abcd-012345678901 for thread '+dispatcher.THREAD+'.','')
        with patch.object(dispatcher,'smtp_mode',return_value=True):
            self.assertEqual(dispatcher.dispatch({**self.event,'stage':'listed'},run=run)['stage'],'ignored')
            self.assertEqual(calls,[])
            self.assertEqual(dispatcher.dispatch(self.event,run=run)['stage'],'queued')
            self.assertIn('聊天不得使用Gmail',calls[0][-1])


if __name__=='__main__':
    unittest.main()
