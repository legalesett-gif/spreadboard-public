from pathlib import Path

from spreadarb.dex import okx_quotes


def _credentials() -> okx_quotes.OkxDexCredentials:
    return okx_quotes.OkxDexCredentials(
        api_key="public-id",
        secret="test-secret",
        passphrase="test-passphrase",
    )


def test_signed_get_retries_default_client_rate_limit(monkeypatch) -> None:
    responses = [
        {"code": "50011", "msg": "Too Many Requests"},
        {"code": "0", "data": []},
    ]
    calls = []

    def fake_http_get(url, headers):
        calls.append((url, headers))
        return responses.pop(0)

    monkeypatch.setattr(okx_quotes, "_http_get", fake_http_get)
    result = okx_quotes._signed_get(
        params={"chainIndex": "1"},
        credentials=_credentials(),
        http_get=None,
    )

    assert result["code"] == "0"
    assert len(calls) == 2
    assert all(call[1]["OK-ACCESS-SIGN"] for call in calls)


def test_signed_get_does_not_retry_injected_test_client() -> None:
    calls = []

    def fake_http_get(url, headers):
        calls.append((url, headers))
        return {"code": "50011", "msg": "Too Many Requests"}

    result = okx_quotes._signed_get(
        params={"chainIndex": "1"},
        credentials=_credentials(),
        http_get=fake_http_get,
    )

    assert result["code"] == "50011"
    assert len(calls) == 1


def test_shared_request_slot_persists_process_rate_state(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "okx-rate.state"
    moments = iter((10.25, 11.15))
    sleeps = []
    monkeypatch.setattr(okx_quotes, "OKX_DEX_RATE_STATE_PATH", str(state_path))
    monkeypatch.setattr(okx_quotes, "OKX_DEX_MIN_REQUEST_INTERVAL_SECONDS", 1.15)
    monkeypatch.setattr(okx_quotes.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(okx_quotes.time, "sleep", sleeps.append)

    state_path.write_text("10.0", encoding="ascii")
    okx_quotes._wait_for_shared_request_slot()

    assert [round(value, 6) for value in sleeps] == [0.9]
    assert state_path.read_text(encoding="ascii") == "11.15"
