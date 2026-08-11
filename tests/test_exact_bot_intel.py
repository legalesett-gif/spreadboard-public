from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from spreadboard import intel


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "sync_exact_bot_intel.py"
SPEC = importlib.util.spec_from_file_location("sync_exact_bot_intel", SCRIPT_PATH)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


def _message(text: str, *, minute: int, outgoing: bool) -> SimpleNamespace:
    return SimpleNamespace(
        message=text,
        date=datetime(2026, 8, 11, 12, minute, tzinfo=timezone.utc),
        out=outgoing,
    )


def test_exact_bot_bridge_keeps_only_anonymous_token_attention() -> None:
    events = bridge.sanitize_messages(
        [
            _message("$GUA funding", minute=1, outgoing=True),
            _message("GUA · funding routes", minute=2, outgoing=False),
        ],
        bot_username="SpreadArbitrageBot",
    )
    assert len(events) == 1
    event = events[0]
    assert event["parsed"]["symbol"] == "GUA"
    assert event["parsed"]["kind"] == "FUNDING"
    serialized = json.dumps(event).casefold()
    for forbidden in ("chat_id", "user_id", "message_id", "username", "email", '"text"'):
        assert forbidden not in serialized


def test_external_exact_bot_events_feed_the_same_intel_surface(tmp_path, monkeypatch) -> None:
    external = tmp_path / "external.jsonl"
    subscription = tmp_path / "subscription-bot.jsonl"
    external.write_text(
        json.dumps(bridge.attention_event("VANRY", "funding", at=1_786_446_000)) + "\n"
    )
    subscription.write_text(
        json.dumps(bridge.attention_event("SIREN", "funding", at=1_786_446_000)) + "\n"
    )
    monkeypatch.setattr(
        intel.board,
        "load_board",
        lambda *_a, **_k: SimpleNamespace(
            rows=[],
            source_path=str(tmp_path / "missing-board.json"),
            age_min=None,
            fresh_count=0,
            stale_count=0,
            error="missing",
        ),
    )
    payload = intel.build_intel(
        board_path=tmp_path / "missing-board.json",
        events_path=subscription,
        external_bot_events_path=external,
        brief_dir=tmp_path / "briefs",
        preflight_candidates_path=tmp_path / "preflight.jsonl",
        strategy_queue_path=tmp_path / "queue.jsonl",
        strategy_prompts_path=tmp_path / "prompts.jsonl",
        private_preflight_path=tmp_path / "private.jsonl",
        digest_path=tmp_path / "digest.jsonl",
        now=1_786_446_300,
    )
    assert payload["hot_symbols"][0]["symbol"] == "VANRY"
    assert all(item["symbol"] != "SIREN" for item in payload["hot_symbols"])
    assert payload["source_freshness"]["telegram_events"]["status"] == "fresh"


def test_empty_sync_is_zero_bytes_and_remote_copy_is_container_readable(
    tmp_path, monkeypatch
) -> None:
    output = tmp_path / "external.jsonl"
    body = bridge._write_atomic(output, [])
    assert body == b""
    assert output.read_bytes() == b""
    assert output.stat().st_mode & 0o777 == 0o600

    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bridge.subprocess, "run", run)
    bridge._sync_remote(
        body,
        host="root@example.test",
        key_path=tmp_path / "ssh-key",
        remote_path="/opt/spreadboard/runtime/community/external_bot_events.jsonl",
    )
    remote_command = captured["command"][-1]
    assert "chmod 0644" in remote_command
    assert captured["kwargs"]["input"] == b""
