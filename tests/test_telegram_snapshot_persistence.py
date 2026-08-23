from __future__ import annotations

import json

from spreadboard import telegram_queries


def test_last_complete_snapshots_restore_across_process_state_reset(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SPREADBOARD_TELEGRAM_SNAPSHOT_DIR", str(tmp_path))
    telegram_queries.reset_payload()
    spread = {"groups": [{"token": "GUA", "routes": [{"route_key": "spread"}]}]}
    funding = {
        "groups": [{"token": "GUA", "routes": [{"route_key": "funding"}]}]
    }

    telegram_queries.replace_payload(spread)
    telegram_queries.replace_funding_payloads([funding])
    assert (tmp_path / "telegram_spread_snapshot.json").is_file()
    assert (tmp_path / "telegram_funding_snapshot.json").is_file()

    telegram_queries.reset_payload()
    restored = telegram_queries.restore_persisted_payloads()

    assert restored == {"spread": True, "funding": True}
    assert telegram_queries.client_visible_payload() == spread
    status = telegram_queries.payload_status()
    assert status["ready"] is True
    assert status["funding_ready"] is True
    assert status["token_count"] == 1
    assert status["funding_token_count"] == 1


def test_corrupt_or_empty_persisted_snapshot_is_ignored(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SPREADBOARD_TELEGRAM_SNAPSHOT_DIR", str(tmp_path))
    telegram_queries.reset_payload()
    (tmp_path / "telegram_spread_snapshot.json").write_text(
        "not-json", encoding="utf-8"
    )
    (tmp_path / "telegram_funding_snapshot.json").write_text(
        json.dumps(
            {
                "schema": "spreadboard.telegram_snapshot.v1",
                "saved_at": 1,
                "payload": {"groups": []},
            }
        ),
        encoding="utf-8",
    )

    assert telegram_queries.restore_persisted_payloads() == {
        "spread": False,
        "funding": False,
    }
    assert telegram_queries.payload_status()["ready"] is False
    assert telegram_queries.payload_status()["funding_ready"] is False
