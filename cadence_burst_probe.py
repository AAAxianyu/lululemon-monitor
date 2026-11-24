#!/usr/bin/env python3
"""Bounded subsecond trial: 3 x 10 rounds, at most 20 HTTP requests in flight.

This is an explicit manual test, never part of the scheduled monitor.
Each round uses a separate evidence directory to avoid concurrent file writes.
"""
import concurrent.futures as futures
import json
from pathlib import Path
import statistics
import time

from availability_notifier import START, utcnow
from lululemon_monitor import check


def one_round(directory, number):
    began = time.monotonic()
    stamp = utcnow().isoformat()
    result = check(directory / f'round-{number:02}', us_size='XS')
    return {'round': number, 'started_monotonic': began, 'started_at': stamp,
            'duration_seconds': round(time.monotonic() - began, 3),
            'failed': bool(result.get('has_request_error') or result.get('status') == 'query_failed'),
            'catalog_status': result['status'],
            'requests': [{key: request.get(key) for key in
                          ('label', 'url', 'status_code', 'error', 'response_headers', 'body_sha256', 'body_file')}
                         for request in result['requests']]}


def run():
    if utcnow() >= START:
        raise SystemExit('Trial disabled once scheduled monitoring starts')
    output = Path(__file__).resolve().parent / 'monitor_data' / ('cadence-burst-' + utcnow().strftime('%Y%m%dT%H%M%SZ'))
    output.mkdir(parents=True, exist_ok=False)
    report = {'started_at': utcnow().isoformat(), 'request_count_limit': 60,
              'rounds_overlap': True, 'concurrent_http_upper_bound': 20,
              'stages': [], 'stopped_reason': None}
    for interval in (0.5, 0.2, 0.1):
        rows, pending = [], set()
        maximum = 0
        began = next_launch = time.monotonic()
        launched = 0
        with futures.ThreadPoolExecutor(max_workers=10) as pool:
            while launched < 10 or pending:
                done = {job for job in pending if job.done()}
                pending -= done
                for job in done:
                    row = job.result()
                    rows.append(row)
                    if row['failed']:
                        report['stopped_reason'] = 'First HTTP/transport/validation error; stop adding requests'
                if utcnow() >= START:
                    report['stopped_reason'] = 'Scheduled monitor window started'
                if report['stopped_reason']:
                    # Already issued requests are allowed to return; never retry/escalate.
                    rows.extend(job.result() for job in pending)
                    pending.clear()
                    break
                if launched < 10 and time.monotonic() >= next_launch:
                    launched += 1
                    pending.add(pool.submit(one_round, output / f'interval-{interval}', launched))
                    maximum = max(maximum, len(pending))
                    next_launch = time.monotonic() + interval
                if pending:
                    timeout = max(0, next_launch - time.monotonic()) if launched < 10 else 1
                    futures.wait(pending, timeout=timeout, return_when=futures.FIRST_COMPLETED)
                elif launched < 10:
                    time.sleep(max(0, next_launch - time.monotonic()))
        rows.sort(key=lambda row: row['started_monotonic'])
        gaps = [b['started_monotonic'] - a['started_monotonic'] for a, b in zip(rows, rows[1:])]
        durations = [row['duration_seconds'] for row in rows]
        counts = {}
        for row in rows:
            row.pop('started_monotonic')
            for request in row['requests']:
                key = str(request['status_code'])
                counts[key] = counts.get(key, 0) + 1
        summary = {'target_interval_seconds': interval, 'rounds': len(rows),
                   'max_rounds_in_flight': maximum, 'http_counts': counts,
                   'elapsed_seconds': round(time.monotonic() - began, 3),
                   'round_median_seconds': round(statistics.median(durations), 3) if durations else None,
                   'round_max_seconds': max(durations, default=None),
                   'actual_start_gap_median_seconds': round(statistics.median(gaps), 3) if gaps else None,
                   'actual_start_gap_min_seconds': round(min(gaps), 3) if gaps else None,
                   'actual_start_gap_max_seconds': round(max(gaps), 3) if gaps else None}
        report['stages'].append({**summary, 'observations': rows})
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(summary), flush=True)
        if report['stopped_reason']:
            break
        if interval != 0.1:
            time.sleep(5)
    report['finished_at'] = utcnow().isoformat()
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'report_file': str(output / 'report.json'),
                      'stopped_reason': report['stopped_reason']}), flush=True)


if __name__ == '__main__':
    run()
