"""A repeated renderer reuses history only until file or settlement changes."""
import json
import os

from spreadboard import bulk_quotes
from spreadboard import venue_funding_history as history


def test_reuse_obeys_live_schedule_rotation_and_exact_settlement_boundary(monkeypatch, tmp_path):
    latest = 1_700_000_000_000
    clock = [latest + 3_600_000]
    key = 'OKX|ICX/USDT:USDT'
    path = tmp_path / 'history.json'
    funding_path = tmp_path / 'funding.json'
    funding_path.write_text('{}')
    values = {'1d': 1., '7d': 7., '30d': 30.}
    details = {label: {'complete': True, 'latest_event_at': latest,
                       'inferred_interval_hours': 8} for label in values}
    path.write_text(json.dumps({'schema':history.SCHEMA, 'legs':{key:values},
        'leg_status':{key:{'status':'ok','window_details':details}}}))
    monkeypatch.setattr(history,'DEFAULT_CACHE_PATH',path)
    monkeypatch.setattr(bulk_quotes,'FUNDING_CACHE_PATH',funding_path)
    monkeypatch.setattr(history,'_CACHE',{'stamp':None,'legs':{},'current_legs':{},
        'leg_status':{},'leg_updated_at':{},'current_until_ms':None,'funding_stamp':None})
    monkeypatch.setattr(history.time,'time',lambda:clock[0]/1000)
    live = {'interval_hours':8,'next_funding_ts_us':(latest+8*3_600_000)*1000}
    reads = []
    def funding():
        reads.append(1)
        return {key:dict(live)}
    monkeypatch.setattr(bulk_quotes,'load_funding',funding)
    for _ in range(200):
        assert history.load(cache_path=path)[key] == values
    assert len(reads) == 1
    # A newly published two-hour venue schedule must supersede eight-hour
    # archive cadence immediately, even though the old window has not expired.
    live.update(interval_hours=2,next_funding_ts_us=(latest+2*3_600_000)*1000)
    stamp=funding_path.stat().st_mtime_ns
    os.utime(funding_path,ns=(stamp+1_000_000,stamp+1_000_000))
    assert history.load(cache_path=path)[key] == values
    assert len(reads) == 2
    clock[0] = latest + 2*3_600_000
    assert history.load(cache_path=path)[key] == {label:None for label in values}
    assert len(reads) == 3
