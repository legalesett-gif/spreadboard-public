from io import BytesIO
from pathlib import Path

import pytest

from spreadarb.dex import okx_quotes


def _credentials() -> okx_quotes.OkxDexCredentials:
    return okx_quotes.OkxDexCredentials(
        api_key="public-id",
        secret="test-secret",
        passphrase="test-passphrase",
    )


def test_dex_credentials_do_not_fall_back_to_exchange_trading_key(monkeypatch) -> None:
    values = {
        "SPREADARB/okx/api_key": "exchange-key",
        "SPREADARB/okx/secret": "exchange-secret",
        "SPREADARB/okx/passphrase": "exchange-passphrase",
    }
    monkeypatch.setattr(okx_quotes, "keychain", values.get)

    assert okx_quotes.load_okx_dex_credentials() is None


def test_provider_access_failure_is_sanitized() -> None:
    assert okx_quotes._provider_blocker(
        {"code": "50100", "msg": "Your API key or regions have no access to current services"},
        "tokens",
    ) == "okx_dex_api_access_denied"
    assert okx_quotes._provider_blocker(
        {"code": "50101", "msg": "Invalid Authority"},
        "quote",
    ) == "okx_dex_api_access_denied"


def test_payment_required_stays_in_explicit_free_mode() -> None:
    assert okx_quotes._provider_blocker(
        {"code": "402", "msg": "Payment Required", "http_status": 402},
        "quote",
    ) == "okx_dex_payment_required_free_mode"


def test_signed_quotes_never_attach_an_x402_payment_credential() -> None:
    calls = []

    def fake_http_get(url, headers):
        calls.append((url, headers))
        return {"code": "0", "data": []}

    okx_quotes._signed_get(
        params={"chainIndex": "1"},
        credentials=_credentials(),
        http_get=fake_http_get,
    )

    assert len(calls) == 1
    assert not any("payment" in name.casefold() for name in calls[0][1])


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


def test_signed_get_retries_default_client_transport_failure_once(
    monkeypatch,
) -> None:
    responses = [
        {"code": "url_error", "msg": "temporary DNS failure"},
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


def test_signed_get_caps_repeated_upstream_failure_at_two_attempts(monkeypatch) -> None:
    calls = []

    def fake_http_get(url, headers):
        calls.append((url, headers))
        return {"code": "500", "msg": "upstream unavailable", "http_status": 503}

    monkeypatch.setattr(okx_quotes, "_http_get", fake_http_get)
    result = okx_quotes._signed_get(
        params={"chainIndex": "1"},
        credentials=_credentials(),
        http_get=None,
    )

    assert result["http_status"] == 503
    assert len(calls) == 2


def test_signed_get_does_not_retry_non_transient_provider_rejection(monkeypatch) -> None:
    calls = []

    def fake_http_get(url, headers):
        calls.append((url, headers))
        return {"code": "51000", "msg": "insufficient liquidity"}

    monkeypatch.setattr(okx_quotes, "_http_get", fake_http_get)
    result = okx_quotes._signed_get(
        params={"chainIndex": "1"},
        credentials=_credentials(),
        http_get=None,
    )

    assert result["code"] == "51000"
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


@pytest.mark.parametrize("saved_clock", ["2829213.908839341", "inf", "nan"])
def test_http_request_recovers_persisted_clock_after_reboot(
    tmp_path: Path, monkeypatch, saved_clock: str
) -> None:
    state_path = tmp_path / "okx-rate.state"
    state_path.write_text(saved_clock, encoding="ascii")
    moments = iter((100.0, 100.55, 100.65, 101.10))
    sleeps = []
    sent_after_clock = []
    monkeypatch.delenv("SPREADBOARD_OKX_DEX_BACKGROUND", raising=False)
    monkeypatch.setattr(okx_quotes, "OKX_DEX_RATE_STATE_PATH", str(state_path))
    monkeypatch.setattr(okx_quotes, "OKX_DEX_MIN_REQUEST_INTERVAL_SECONDS", 0.55)
    monkeypatch.setattr(okx_quotes.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(okx_quotes.time, "sleep", sleeps.append)

    def http_response(request, *, timeout):
        assert timeout == 15
        sent_after_clock.append(float(state_path.read_text(encoding="ascii")))
        return BytesIO(b'{"code":"0","data":[]}')

    monkeypatch.setattr(okx_quotes, "urlopen", http_response)
    for _ in range(2):
        assert okx_quotes._http_get("https://example.invalid/quote", {})["code"] == "0"

    # Reboot recovery waits a full provider interval; the next request still
    # shares the newly persisted rate state instead of bypassing the limiter.
    assert sleeps == pytest.approx([0.55, 0.45])
    assert sent_after_clock == [100.55, 101.10]


def test_background_discovery_yields_to_current_board_quote_window(
    tmp_path: Path, monkeypatch
) -> None:
    priority_path = tmp_path / "okx-priority.state"
    rate_path = tmp_path / "okx-rate.state"
    priority_path.write_text("fast-worker", encoding="ascii")
    sleeps = []

    def release_after_first_wait(seconds: float) -> None:
        sleeps.append(seconds)
        priority_path.unlink(missing_ok=True)

    monkeypatch.setenv("SPREADBOARD_OKX_DEX_BACKGROUND", "1")
    monkeypatch.setattr(okx_quotes, "OKX_DEX_PRIORITY_STATE_PATH", str(priority_path))
    monkeypatch.setattr(okx_quotes, "OKX_DEX_RATE_STATE_PATH", str(rate_path))
    monkeypatch.setattr(okx_quotes, "OKX_DEX_MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(okx_quotes.time, "sleep", release_after_first_wait)

    okx_quotes._wait_for_shared_request_slot()

    assert sleeps == [0.25]
