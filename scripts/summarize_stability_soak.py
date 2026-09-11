"""Summarize finite observation without treating short/reset windows as passes."""
import argparse
import collections
import datetime
import json
import math
import statistics
from itertools import pairwise
from pathlib import Path

EXPECTED_CONTAINERS = {'app-app-1', 'app-collector-1', 'app-accounting-worker-1'}


def valid_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('samples', type=Path, help='Append-only stability_soak.py samples.jsonl')
args = parser.parse_args()
rows = [json.loads(line) for line in args.samples.read_text().splitlines()]
hosts = [r for r in rows if r['kind'] == 'host']
if not hosts:
    print(json.dumps({'runtime_48h_pass': False, 'reason': 'no_host_samples'}))
    raise SystemExit(0)
def container_ids(row):
    containers = row.get('containers') or []
    if not containers or any(not c.get('name') or not c.get('container_id') for c in containers):
        return None
    result = {c['name']: c['container_id'] for c in containers}
    return result if (set(result) == EXPECTED_CONTAINERS
                      and len(result) == len(containers)
                      and len(set(result.values())) == len(result)) else None
latest_ids = container_ids(hosts[-1])
if latest_ids is None:
    print(json.dumps({'runtime_48h_pass': False, 'reason': 'latest_container_identity_missing'}))
    raise SystemExit(0)
def same_release(row):
    return container_ids(row) == latest_ids
start_index = len(hosts) - 1
while start_index > 0 and same_release(hosts[start_index - 1]):
    start_index -= 1
generation_start = hosts[start_index]['at']
observer_start = max((r['at'] for r in rows if r['kind'] == 'start'), default=generation_start)
segment = [r for r in hosts[start_index:] if r['at'] >= observer_start]
if not segment:
    print(json.dumps({'runtime_48h_pass': False, 'reason': 'new_observer_has_no_host_samples'}))
    raise SystemExit(0)
start, end = segment[0]['at'], segment[-1]['at']
latest_limits = {c['name']: c.get('memory_limit_bytes') for c in hosts[-1]['containers']}
config_start = len(segment) - 1
while config_start > 0 and {c['name']: c.get('memory_limit_bytes') for c in segment[config_start - 1]['containers']} == latest_limits:
    config_start -= 1
configuration_started = segment[config_start]['at']
segment = segment[config_start:]
start = configuration_started
endpoints = [r for r in rows if r['kind'] == 'endpoint' and start <= r['requested_at'] <= end]
health = [r for r in endpoints if r['path'] == '/api/health']
counts = [r.get('universe', {}).get('current_priced_route_count') for r in health]
counts = [n for n in counts if valid_number(n)]
median = statistics.median(counts) if counts else None
swing = max(abs(n / median - 1) for n in counts) if median else None
route_hour = None
for i in range(max(0, len(health)-29)):
    sample = health[i:i+30]
    span = sample[-1]['requested_at'] - sample[0]['requested_at']
    values = [r.get('universe', {}).get('current_priced_route_count') for r in sample]
    if not 3480 <= span <= 3720 or any(not valid_number(v) or v <= 0 for v in values):
        continue
    center = statistics.median(values)
    deviation = max(abs(v/center-1) for v in values)
    if all(r.get('code') == 200 for r in sample) and deviation <= .10:
        route_hour = {'start': sample[0]['requested_at'], 'end': sample[-1]['requested_at'],
                      'samples': 30, 'deviation_pct': round(deviation*100, 3)}
        break
host_gap = max((b['at']-a['at'] for a,b in pairwise(segment)), default=0)
endpoint_gaps = {}
for path in ('/api/health', '/free'):
    moments = [start] + sorted(r['requested_at'] for r in endpoints if r['path'] == path) + [end]
    endpoint_gaps[path] = max((b-a for a,b in pairwise(moments)), default=end-start)
memory = {}
for name in latest_ids:
    points = [(r['at'], c) for r in segment for c in r['containers'] if c['name'] == name]
    # Conservative bound: include the interval from the last healthy sample
    # through the next healthy sample, not just the observed bad timestamps.
    last_healthy, bad_since, longest_bad = start, None, 0.0
    for at, c in points:
        if c.get('health') == 'healthy':
            if bad_since is not None:
                longest_bad = max(longest_bad, at-bad_since)
                bad_since = None
            last_healthy = at
        elif bad_since is None:
            bad_since = last_healthy
    if bad_since is not None:
        longest_bad = max(longest_bad, end-bad_since)
    memory_valid = all(
        all(valid_number(c.get(field)) and c[field] > 0 for field in
            ('memory_anon_bytes', 'memory_peak_bytes', 'memory_limit_bytes'))
        for _, c in points
    )
    cpu_values = [(c.get('cpu') or {}).get('usage_usec') for _, c in points]
    cpu_valid = all(valid_number(value) for value in cpu_values)
    cpu_valid = cpu_valid and all(b >= a for a, b in pairwise(cpu_values))
    runtime_valid = all(c.get('health') in ('healthy', 'unhealthy', 'starting')
                        and valid_number(c.get('cgroup_oom_kill'))
                        and valid_number(c.get('restart_count')) for _, c in points)
    memory[name] = {
        'max_unhealthy_bound_seconds': round(longest_bad, 2),
        'started_healthy': points[0][1].get('health') == 'healthy',
        'ended_healthy': points[-1][1].get('health') == 'healthy',
        'all_runtime_samples_valid': runtime_valid,
        'all_memory_samples_valid': memory_valid,
        'all_cpu_samples_valid': cpu_valid,
        'oom_or_restart_observed': any(c.get('cgroup_oom_kill') != 0 or c.get('restart_count') != 0 for _, c in points),
        'anon_peak_mib': round(max(c['memory_anon_bytes'] for _, c in points) / 2**20, 1) if memory_valid else None,
        'kernel_peak_mib': round(points[-1][1]['memory_peak_bytes'] / 2**20, 1) if memory_valid else None,
        'cpu_cores': round((cpu_values[-1] - cpu_values[0]) / max(end-start, 1) / 1e6, 3) if cpu_valid else None,
        'oom_kill': points[-1][1].get('cgroup_oom_kill'),
        'restarts': points[-1][1].get('restart_count'),
        'nonhealthy_samples': sum(c.get('health') != 'healthy' for _, c in points),
    }
backups = {r['ExecMainExitTimestamp']: r for r in rows if r['kind'] == 'backup'
           and r.get('ActiveState') == 'inactive' and r.get('Result') == 'success'
           and r.get('ExecMainStatus') == '0' and r.get('ExecMainExitTimestamp')}
result = {
    'last_sample_utc': datetime.datetime.fromtimestamp(end, datetime.UTC).isoformat(),
    'latest_container_ids': latest_ids,
    'stable_generation_minutes': round((end-generation_start)/60, 2),
    'observer_start_utc': datetime.datetime.fromtimestamp(observer_start, datetime.UTC).isoformat(),
    'host_samples': len(segment),
    'max_host_gap_seconds': round(host_gap, 2),
    'priced_samples': len(counts),
    'priced_min': min(counts) if counts else None,
    'priced_max': max(counts) if counts else None,
    'max_deviation_from_median_pct': round(swing*100, 3) if swing is not None else None,
    'route_hour_pass': route_hour is not None,
    'route_hour_evidence': route_hour,
    'max_endpoint_gaps_seconds': endpoint_gaps,
    'endpoint_failures': [r for r in endpoints if r.get('code') != 200],
    'endpoint_counts': dict(collections.Counter(r['path'] for r in endpoints)),
    'memory_cpu': memory,
    'completed_backup_exits': sorted(backups),
    'backup_note': 'Completed exits still require distinguishing normal timer runs from manual catch-up.',
    'current_memory_limits': latest_limits,
    'stable_configuration_minutes': round((end-configuration_started)/60, 2),
    'duration_48h_reached': end-configuration_started >= 48*3600,
    'latest_funding_history': health[-1].get('funding_history') if health else None,
}
result['runtime_48h_pass'] = bool(
    result['duration_48h_reached'] and route_hour and host_gap <= 30
    and not result['endpoint_failures']
    and all(result['endpoint_counts'].get(p, 0) > 0 and gap <= 330 for p,gap in endpoint_gaps.items())
    and all(v['all_runtime_samples_valid'] and v['all_memory_samples_valid']
            and v['all_cpu_samples_valid'] and not v['oom_or_restart_observed']
            and v['started_healthy'] and v['ended_healthy']
            and v['max_unhealthy_bound_seconds'] <= 90
            and v['oom_kill'] == 0 and v['restarts'] == 0 for v in memory.values())
)
result['acceptance_scope'] = 'Runtime only. Backups, safe caps, source parity and product requirements remain separate gates.'
print(json.dumps(result, indent=2))
