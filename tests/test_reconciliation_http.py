from __future__ import annotations

import http.client
import json
import threading

from spreadboard import materialized_views, server


def test_reconciliation_endpoint_is_token_scoped_and_read_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SPREADBOARD_AUTH_REQUIRED", "1")
    monkeypatch.setenv("SPREADBOARD_RECONCILIATION_TOKEN", "correct-monitor-token")

    class Store:
        def live_route_index(self, *, board_path):
            del board_path
            return {"route": {"route_key": "route"}}

    monkeypatch.setattr(materialized_views, "default_store", lambda: Store())
    observed = {}

    def run(payload, *, routes, catalog):
        observed.update({"payload": payload, "routes": routes, "catalog": catalog})
        return {
            "release_gate_passed": True,
            "exact_pair_recall_pct": 100.0,
            "recall_drop_pp": 0.0,
            "absence_count": 0,
            "spread_investigation_count": 0,
            "failures": [],
        }

    monkeypatch.setattr(server.coverage_reconciliation, "run", run)
    monkeypatch.setattr(server.chart_catalog, "load", lambda: {"markets": []})
    monkeypatch.setattr(
        server.operator_alerts, "notify_transition", lambda *_args, **_kwargs: {"changed": False}
    )
    app = server.SpreadBoardServer(
        ("127.0.0.1", 0),
        server.SpreadBoardHandler,
        board_path=tmp_path / "board.jsonl",
        config={},
        accounts_path=tmp_path / "accounts.sqlite3",
    )
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    client = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=10)
    payload = {
        "source": "uacryptoinvest.com",
        "rows": [
            {
                "token": "GUA",
                "long_venue": "Mexc",
                "long_market_type": "Futures",
                "short_venue": "Gate",
                "short_market_type": "Futures",
            }
        ],
    }
    try:
        client.request(
            "POST",
            "/api/internal/reconciliation/uacryptoinvest",
            body=json.dumps(payload),
            headers={"Content-Type": "application/json", "Authorization": "Bearer wrong"},
        )
        response = client.getresponse()
        assert response.status == 401
        response.read()

        client.request(
            "POST",
            "/api/internal/reconciliation/uacryptoinvest",
            body=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer correct-monitor-token",
            },
        )
        response = client.getresponse()
        result = json.loads(response.read())
        assert response.status == 200
        assert result["release_gate_passed"] is True
        assert observed["routes"] == {"route": {"route_key": "route"}}
    finally:
        client.close()
        app.shutdown()
        app.server_close()
        thread.join(timeout=5)
