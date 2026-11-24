import copy
import datetime as dt
import tempfile
import time
import threading
import unittest
from pathlib import Path

from availability_notifier import (Outbox, START, END, classify, confirm_event, backoff,
                                   mail_for, scheduled_interval, next_sleep, run_window, utcnow)
from test_checkout import fixture


def result():
    data=fixture()
    data['target_skus'][0]['sku']='159499999'
    return data


class NotificationTests(unittest.TestCase):
    def test_time_window_is_explicit_beijing(self):
        self.assertEqual(START.astimezone(dt.timezone.utc).isoformat(),'2026-11-16T16:50:00+00:00')
        self.assertEqual(END.astimezone(dt.timezone.utc).isoformat(),'2027-11-16T22:00:00+00:00')
        self.assertGreater((END-START).days,360)

    def test_only_exact_classic_lavr_xs_inventory(self):
        self.assertEqual(classify(result())['stage'],'xs_available')
        for changes in ({'product_id':'oversized'},{'product_name':'Scuba Evolve Oversized Full-Zip Hoodie'}, {'status':'query_failed'}):
            self.assertEqual(classify({**result(),**changes})['stage'],'query_failed')
        for color in ({'id':'75787','name':'Perfect Plumful/Gold','colorGroup':'PPGF'},
                      {'id':'0001','name':'Black','colorGroup':'BLK'}):
            data=result(); data['target_skus'][0]['color']=color
            self.assertEqual(classify(data)['stage'],'not_listed')
        # User decision 2026-11-17: Lavender Rock zipper combinations count as the target.
        for color in ({'id':'77631','name':'Lavender Rock/Gold','colorGroup':'LAVR'},
                      {'id':'81999','name':'Lavender Rock/Gold','colorGroup':'LAVR/GOLD'},
                      {'id':'81998','name':'Lavender Rock/Mirror Silver','colorGroup':'LAVR/MRRS'}):
            data=result(); data['target_skus'][0]['color']=color
            self.assertEqual(classify(data)['stage'],'xs_available')
        # Name/group disagreement still notifies but never auto-checks out.
        data=result(); data['target_skus'][0]['color']={'id':'81997','name':'Lavender Rock','colorGroup':'PPGF'}
        self.assertEqual(classify(data)['stage'],'listed')

    def test_metadata_without_sku_is_not_listing(self):
        data=result(); data.update(target_skus=[],target_colors=[{'id':'77631','name':'Lavender Rock','colorGroup':'LAVR'}])
        self.assertEqual(classify(data)['stage'],'not_listed')

    def test_listing_separate_from_xs_and_stock_error(self):
        for change in ({'is_available':False},{'is_available':None},{'au_size':'8-10'},
                       {'matches_requested_us_size':False},{'stock_source':'unknown'}):
            data=result(); data['target_skus'][0].update(change)
            self.assertEqual(classify(data)['stage'],'listed')
        data=result(); data['has_request_error']=True
        self.assertEqual(classify(data)['stage'],'listed')

    def test_second_observation_must_confirm_identity_and_availability(self):
        good=result()
        self.assertEqual(confirm_event(good,good)['stage'],'xs_available')
        changed=copy.deepcopy(good); changed['target_skus'][0]['sku']='123450000'
        self.assertIsNone(confirm_event(good,changed))
        missing=copy.deepcopy(good); missing['target_skus']=[]
        self.assertIsNone(confirm_event(good,missing))
        error=copy.deepcopy(good); error['has_request_error']=True
        self.assertEqual(confirm_event(good,error)['stage'],'listed')

    def test_retry_after_seconds_and_date(self):
        now=dt.datetime(2026,11,16,17,tzinfo=dt.timezone.utc)
        self.assertEqual(backoff({'requests':[{'response_headers':{'retry-after':'120'}}]},1,now=now),120)
        self.assertEqual(backoff({'requests':[{'response_headers':{'retry-after':'Mon, 16 Nov 2026 17:05:00 GMT'}}]},1,now=now),300)
        self.assertEqual(backoff({},0),0.1)
        self.assertEqual(backoff({},1,minimum=2),30)
        self.assertEqual(backoff({},2,minimum=2),60)
        self.assertEqual(backoff({},12,minimum=2),900)

    def test_peak_schedule_in_beijing_and_utc(self):
        for clock, expected in (('00:49:59',0.1),('00:50:00',0.1),('01:19:59',0.1),
                                ('01:20:00',0.1),('04:49:59',0.1),('04:50:00',0.1),
                                ('05:19:59',0.1),('05:20:00',0.1)):
            instant=dt.datetime.fromisoformat('2026-11-17T'+clock+'+08:00')
            self.assertEqual(scheduled_interval(instant),expected)
            self.assertEqual(scheduled_interval(instant.astimezone(dt.timezone.utc)),expected)

    def test_sleep_respects_round_duration_boundaries_and_failure_delay(self):
        peak=dt.datetime.fromisoformat('2026-11-17T01:00:00+08:00')
        self.assertAlmostEqual(next_sleep({},0,0.04,peak),0.06)
        self.assertEqual(next_sleep({},0,4,peak),0)  # No overlapping catch-up requests.
        before_peak=dt.datetime.fromisoformat('2026-11-17T04:49:59+08:00')
        self.assertEqual(next_sleep({},0,1,before_peak),0)
        self.assertEqual(next_sleep({},1,25,before_peak),30)  # Don't shorten backoff at a peak.
        limited={'requests':[{'response_headers':{'retry-after':'120'}}]}
        self.assertEqual(next_sleep(limited,1,25,peak),120)
        before_end=END-dt.timedelta(seconds=3)
        self.assertEqual(next_sleep(limited,1,25,before_end),3)

    def test_outbox_persists_deduplicates_and_lease_prevents_two_sends(self):
        with tempfile.TemporaryDirectory() as root:
            box=Outbox(Path(root)); observation=classify(result())
            event=box.enqueue(observation)
            self.assertIsNone(Outbox(Path(root)).enqueue(observation))
            self.assertEqual(len(box.pending()),1)
            item=box.claim(event)
            self.assertEqual(box.pending(),[])
            with self.assertRaises(ValueError): box.claim(event)
            box.sent(event,'actual-message-id',item['lease_token'])
            self.assertEqual(Outbox(Path(root)).pending(),[])
            self.assertEqual(box.stats()[0]['status'],'sent')

    def test_uncertain_send_requires_reconciliation_after_lease(self):
        with tempfile.TemporaryDirectory() as root:
            box=Outbox(root); event=box.enqueue(classify(result())); box.claim(event)
            with box.connect() as db: db.execute('UPDATE events SET updated=?',(time.time()-121,))
            pending=box.pending()[0]
            self.assertTrue(pending['requires_sent_search'])
            self.assertTrue(box.claim(event)['requires_sent_search'])

    def test_test_message_is_explicit_and_does_not_contaminate_target_stages(self):
        with tempfile.TemporaryDirectory() as root:
            box=Outbox(root); box.enqueue({'stage':'test'},control={'color':'Black'})
            event=box.pending()[0]
            self.assertIn('测试邮件',event['mail']['subject'])
            self.assertIn('不是目标 Lavender Rock',event['mail']['payload']['body']['content'])
            self.assertEqual(box.stages(),{'test'})
            self.assertEqual(event['mail']['to'],'alerts@example.com')

    def test_handoff_contains_exact_sku_and_survives_email_ack(self):
        with tempfile.TemporaryDirectory() as root:
            box=Outbox(root)
            self.assertIsNone(box.handoff())
            box.enqueue({'stage':'test'},control={'color':'Black'})
            self.assertIsNone(box.handoff())
            event=box.enqueue(classify(result()))
            item=box.claim(event); box.sent(event,'real-id',item['lease_token'])
            self.assertEqual(box.handoff()['sku'],'159499999')
            self.assertEqual(box.handoff()['stop_at'],'payment')

    def test_production_email_is_prepared_for_both_recipients(self):
        mail=mail_for(classify(result()),'LULU-1234567890abcdef')
        self.assertEqual(mail['to'],'alerts@example.com, recipient2@example.com, recipient3@example.com')
        self.assertIn('Scuba Evolve Classic Full-Zip Hoodie',mail['payload']['body']['content'])
        self.assertIn('color=77631&amp;skuId=159499999',mail['payload']['body']['content'])

    def test_pausing_pipeline_drops_pending_positive_events(self):
        paused=threading.Event()
        def fake():
            paused.set()
            return result()
        with tempfile.TemporaryDirectory() as root:
            box=Outbox(root)
            state=run_window(box,end=utcnow()+dt.timedelta(seconds=0.5),check_fn=fake,
                             persist_fn=lambda *_:None,status_fn=lambda stage,**kw:{'stage':stage},
                             paused_fn=paused.is_set)
            self.assertEqual(state['stage'],'paused_by_user')
            self.assertEqual(box.pending(),[])

    def test_pipeline_caps_concurrency_and_does_not_launch_past_end(self):
        lock=threading.Lock()
        active=maximum=0
        starts=[]
        def fake():
            nonlocal active,maximum
            with lock:
                active+=1; maximum=max(active,maximum); starts.append(utcnow())
            time.sleep(0.025)
            with lock: active-=1
            return {**result(),'target_skus':[]}
        with tempfile.TemporaryDirectory() as root:
            end=utcnow()+dt.timedelta(seconds=0.16)
            run_window(Outbox(root),end=end,check_fn=fake,interval_fn=lambda _:0.003,
                       persist_fn=lambda *_:None,status_fn=lambda stage,**kw:{'stage':stage,**kw},max_in_flight=3)
            self.assertEqual(maximum,3)
            self.assertGreater(len(starts),3)
            self.assertTrue(all(stamp<end for stamp in starts))

    def test_rate_limit_is_not_cleared_by_inflight_success(self):
        counter=0
        lock=threading.Lock()
        def fake():
            nonlocal counter
            with lock: counter+=1; number=counter
            time.sleep(0.015 if number==1 else 0.03)
            if number==1:
                return {**result(),'has_request_error':True,'requests':[{'response_headers':{'retry-after':'90'}}]}
            return {**result(),'target_skus':[]}
        statuses=[]
        with tempfile.TemporaryDirectory() as root:
            run_window(Outbox(root),end=utcnow()+dt.timedelta(seconds=0.12),check_fn=fake,
                       interval_fn=lambda _:0.002,persist_fn=lambda *_:None,
                       status_fn=lambda stage,**kw:statuses.append((stage,kw)),max_in_flight=3)
            self.assertEqual(counter,3)
            self.assertTrue(any(stage=='query_failed' and d['next_check_delay_seconds']>89 for stage,d in statuses))

    def test_old_positive_cannot_overwrite_newer_absence(self):
        counter=0; lock=threading.Lock(); evidence=[]
        def fake():
            nonlocal counter
            with lock: counter+=1; number=counter
            time.sleep(0.06 if number==1 else 0.008)
            return result() if number==1 else {**result(),'target_skus':[]}
        with tempfile.TemporaryDirectory() as root:
            box=Outbox(root)
            run_window(box,end=utcnow()+dt.timedelta(seconds=0.1),check_fn=fake,
                       interval_fn=lambda _:0.003,persist_fn=lambda r,latest:evidence.append((r['poll_sequence'],latest)),
                       status_fn=lambda *_args,**_kw:None,max_in_flight=3)
            self.assertEqual(box.pending(),[])
            self.assertIn((1,False),evidence)

    def test_positive_confirmation_starts_only_after_pipeline_drains(self):
        active=0; lock=threading.Lock(); starts=[]; finishes=[]
        def fake():
            nonlocal active
            with lock:
                active+=1; starts.append(time.monotonic())
            time.sleep(0.025)
            with lock:
                active-=1; finishes.append(time.monotonic())
            return result()
        with tempfile.TemporaryDirectory() as root:
            box=Outbox(root)
            status=run_window(box,end=utcnow()+dt.timedelta(seconds=0.5),check_fn=fake,
                             interval_fn=lambda _:0.003,persist_fn=lambda *_:None,
                             status_fn=lambda stage,**kw:{'stage':stage,**kw},max_in_flight=3)
            self.assertEqual(status['stage'],'target_available')
            self.assertEqual(len(starts),4)
            self.assertGreaterEqual(starts[-1],max(finishes[:-1]))
            self.assertEqual(len(box.pending()),1)


if __name__=='__main__': unittest.main()
