import io
import json
import urllib.error
from types import SimpleNamespace

from scripts import stability_soak as soak


def test_health_probe_records_metrics_and_drops_unrelated_payload(monkeypatch):
    payload = {"unrelated_private_field": "do-not-save", "source_health": {
        "materialized_views": {"live_query_universe": {"current_priced_route_count": 12}},
    }}
    response = io.StringIO(json.dumps(payload))
    response.status = 200
    monkeypatch.setattr(soak.urllib.request, "urlopen", lambda *a, **kw: response)
    sample = soak.endpoint_sample("/api/health")
    assert sample["code"] == 200
    assert sample["universe"]["current_priced_route_count"] == 12
    assert "do-not-save" not in json.dumps(sample)


def test_http_failure_keeps_the_real_status(monkeypatch):
    def failed(*a, **kw):
        raise urllib.error.HTTPError("sensitive-url", 503, "unavailable", {}, None)
    monkeypatch.setattr(soak.urllib.request, "urlopen", failed)
    sample = soak.endpoint_sample("/free")
    assert sample["code"] == 503
    assert "sensitive-url" not in json.dumps(sample)


def test_active_backup_is_recorded_as_active_even_with_success_result(monkeypatch):
    monkeypatch.setattr(soak.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout="ActiveState=activating\nResult=success\nExecMainExitTimestamp=\n",
    ))
    sample = soak.backup_sample()
    assert sample["ActiveState"] == "activating"
    assert sample["ExecMainExitTimestamp"] == ""
