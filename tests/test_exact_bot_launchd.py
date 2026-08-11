from pathlib import Path

from scripts import install_exact_bot_intel_launchd as installer


def test_launch_agent_is_bounded_and_contains_no_secret_values(tmp_path: Path) -> None:
    payload = installer.launch_agent_payload(
        uv_path=Path("/opt/uv"),
        session_path=tmp_path / "exact.session",
        output_path=tmp_path / "events.jsonl",
        ssh_host="operator@example",
        ssh_key=tmp_path / "id_key",
        remote_path="/srv/events.jsonl",
        interval_seconds=300,
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )

    assert payload["Label"] == installer.LABEL
    assert payload["StartInterval"] == 300
    assert payload["RunAtLoad"] is True
    command = payload["ProgramArguments"]
    assert "sync_exact_bot_intel.py" in " ".join(command)
    assert "--remote-path" in command
    serialized = repr(payload).casefold()
    for forbidden in ("api_hash", "api_id", "telegram_phone", "password", "2fa"):
        assert forbidden not in serialized
