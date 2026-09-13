import json

import pytest
from scripts.record_deployment import record


def test_partial_release_preserves_other_service_revision(tmp_path):
    record(tmp_path, "a" * 40, "a" * 16, False, ["app", "collector"])
    result = record(tmp_path, "b" * 40, "b" * 16, False, ["app"])
    assert result["services"]["collector"]["revision"] == "a" * 40
    assert result["services"]["app"]["revision"] == "b" * 40
    assert (tmp_path / ".deployed_revision").read_text().strip() == "b" * 40


def test_collector_only_does_not_relabel_web_app(tmp_path):
    record(tmp_path, "a" * 40, "a" * 16, False, ["app"])
    record(tmp_path, "b" * 40, "b" * 16, False, ["collector"])
    assert (tmp_path / ".deployed_revision").read_text().strip() == "a" * 40


def test_dirty_source_is_not_claimed_as_exact_commit(tmp_path):
    record(tmp_path, "a" * 40, "a" * 16, True, ["app"])
    assert (tmp_path / ".deployed_revision").read_text().endswith("-dirty\n")
    assert json.loads((tmp_path / ".deployment_receipt.json").read_text())["services"]["app"]["dirty_source"]


def test_bad_previous_receipt_is_not_silently_replaced(tmp_path):
    path = tmp_path / ".deployment_receipt.json"
    path.write_text('{"schema": 99}')
    with pytest.raises(ValueError):
        record(tmp_path, "a" * 40, "a" * 16, False, ["app"])
    assert path.read_text() == '{"schema": 99}'
