#!/usr/bin/env python3
"""One bounded, read-only cadence trial; never queues mail or touches checkout."""
import datetime as dt
import json
import statistics
import time
from pathlib import Path

from availability_notifier import START, utcnow
from lululemon_monitor import check


def run():
    if utcnow() >= START:
        raise SystemExit('Trial disabled once the scheduled monitoring window starts')
    stamp = utcnow().strftime('%Y%m%dT%H%M%SZ')
    output = Path(__file__).resolve().parent / 'monitor_data' / ('cadence-trial-' + stamp)
    output.mkdir(parents=True, exist_ok=False)
    report = {'started_at': utcnow().isoformat(), 'stages': [], 'stopped_reason': None,
              'request_count_limit': 102, 'rounds_overlap': False}
    for interval, count in ((5, 6), (2, 15), (1, 30)):
        rows, starts = [], []
        stage_start = time.monotonic()
        for index in range(count):
            if utcnow() >= START:
                report['stopped_reason'] = 'Scheduled monitor window started'
                break
            began = time.monotonic()
            starts.append(began)
            result = check(output, us_size='XS')
            elapsed = time.monotonic() - began
            rows.append({'round': index + 1, 'duration_seconds': round(elapsed, 3),
                         'checked_at': result['checked_at_utc'], 'catalog_status': result['status'],
                         'requests': [{key: request.get(key) for key in
                                       ('label', 'url', 'status_code', 'error', 'response_headers', 'body_sha256')}
                                      for request in result['requests']]})
            if result.get('has_request_error') or result.get('status') == 'query_failed':
                report['stopped_reason'] = 'First HTTP, transport, or response-validation failure; no escalation or retry'
                break
            if index + 1 < count:
                time.sleep(max(0, interval - elapsed))
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        durations = [r['duration_seconds'] for r in rows]
        codes = {}
        for row in rows:
            for request in row['requests']:
                key = str(request['status_code'])
                codes[key] = codes.get(key, 0) + 1
        summary = {'target_interval_seconds': interval, 'rounds': len(rows),
                   'elapsed_seconds': round(time.monotonic() - stage_start, 3),
                   'http_counts': codes,
                   'round_median_seconds': round(statistics.median(durations), 3) if durations else None,
                   'round_max_seconds': max(durations, default=None),
                   'actual_start_gap_median_seconds': round(statistics.median(gaps), 3) if gaps else None}
        report['stages'].append({**summary, 'observations': rows})
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(summary), flush=True)
        if report['stopped_reason']:
            break
        # Do not overlap stages, even if the last round was faster than its interval.
        if interval != 1:
            time.sleep(max(0, interval - (durations[-1] if durations else 0)))
    report['finished_at'] = utcnow().isoformat()
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'report_file': str(output / 'report.json'),
                      'stopped_reason': report['stopped_reason']}), flush=True)


if __name__ == '__main__':
    run()
