import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import subprocess
import direct_browser_worker as worker

class DirectTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        for name,value in [('DIRECT',Path(self.temp.name)),('is_paused',lambda:False),
                           ('utcnow',lambda:worker.START+dt.timedelta(seconds=1))]:
            p=patch.object(worker,name,value);p.start();self.addCleanup(p.stop)
        self.handoff={'product_id':'lopmi7nqah','color':'Lavender Rock','us_size':'XS',
                      'quantity':1,'sku':'123456789'}

    def test_event_launches_node_directly_once(self):
        calls=[]
        def run(argv,**kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv,0,json.dumps({'stage':'ready_for_manual_card','submitted':False}),'')
        self.assertEqual(worker.handle(self.handoff,run=run)['stage'],'ready_for_manual_card')
        self.assertEqual(worker.handle(self.handoff,run=run)['stage'],'already_attempted')
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][1:],['direct_checkout.mjs','production','123456789'])

    def test_wrong_target_pause_and_deadline_never_launch(self):
        def forbidden(*a,**kw): self.fail('must not launch')
        with self.assertRaises(ValueError):worker.handle({**self.handoff,'color':'Black'},run=forbidden)
        with patch.object(worker,'is_paused',return_value=True):
            self.assertEqual(worker.handle(self.handoff,run=forbidden)['stage'],'paused_by_user')
        with patch.object(worker,'utcnow',return_value=worker.END):
            self.assertEqual(worker.handle(self.handoff,run=forbidden)['stage'],'outside_window')

    def test_uncertain_result_is_not_retried(self):
        def run(*a,**kw):raise subprocess.TimeoutExpired('node',360)
        self.assertEqual(worker.handle(self.handoff,run=run)['stage'],'execution_unknown')
        self.assertEqual(worker.handle(self.handoff,run=run)['stage'],'already_attempted')

if __name__=='__main__':unittest.main()
