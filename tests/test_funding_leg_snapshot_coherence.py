"""The explanation beside live carry must use the exact same funding snapshot."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from spreadboard.packed_routes import PackedRoutes

from spreadboard import funding_catalog as f
from spreadboard import server


@pytest.fixture
def live_pair(monkeypatch):
    row = {"token": "ONG", "route_key": "A", "route_kind": "FUTURES",
           "long_venue": "Bingx", "short_venue": "Bybit",
           "long_market_type": "Futures", "short_market_type": "Futures",
           "long_market_symbol": "ONG/USDT:USDT", "short_market_symbol": "ONG/USDT:USDT",
           "long_funding_pct": 99, "short_funding_pct": 88,
           "long_funding_interval_hours": 8, "short_funding_interval_hours": 8,
           "long_funding_interval_assumed": True, "short_funding_interval_assumed": True,
           "funding_daily_pct": 99, "executable_spread_pct": 1, "age_min": 0.1}
    rates = {
        "Bingx|ONG/USDT:USDT": {"rate_pct": -.08, "interval_hours": 1,
                               "interval_assumed": False, "age_seconds": 60},
        "Bybit|ONG/USDT:USDT": {"rate_pct": .02, "interval_hours": 4,
                               "interval_assumed": False, "age_seconds": 120},
    }
    monkeypatch.setenv("SPREADBOARD_SERVICE_ROLE", "web")
    monkeypatch.setattr(f, "_complete_payloads", lambda: {"ONG": {"routes": PackedRoutes([row])}})
    monkeypatch.setattr(f, "_resident_live_overlay", lambda batch: batch)
    monkeypatch.setattr(f.bulk_quotes, "load_funding", lambda: rates)
    monkeypatch.setattr(f.venue_funding_history, "load", dict)
    monkeypatch.setattr(f.funding_radar, "routes_for", lambda *a, **kw: [])
    monkeypatch.setattr(f, "_window_value", lambda *a, **kw: .5)
    return row, rates


@pytest.mark.parametrize("surface", ["page", "navigation"])
@pytest.mark.parametrize("window", ["now", "7d"])
def test_ranked_page_and_navigation_explain_their_actual_current_carry(live_pair, surface, window):
    page = (f.page(symbol="ONG", window=window) if surface == "page"
            else f.build_navigation_pages()[("FUTURES", window)])
    row = page["rows"][0]
    assert row["funding_daily_pct"] == pytest.approx(2.04)
    assert row["long_funding_pct"] == -.08
    assert row["short_funding_pct"] == .02
    assert row["long_funding_interval_hours"] == 1
    assert row["short_funding_interval_hours"] == 4
    assert row["long_funding_interval_assumed"] is False
    html = server.render_funding_token_group(page["groups"][0], selected_window=window)
    assert 'data-funding-route-key="A"' in html
    assert 'data-live-funding-leg="long">-0.0800% · every 1h' in html
    assert 'data-live-funding-leg="short">+0.0200% · every 4h' in html
    assert "every 8h" not in html
    if window == "7d":
        assert 'strong data-live-funding>' not in html


def test_an_expired_leg_is_cleared_in_exact_detail(live_pair):
    _, rates = live_pair
    del rates["Bingx|ONG/USDT:USDT"]
    row = f.page(symbol="ONG")["rows"][0]
    assert row["funding_daily_pct"] is None
    assert row["long_funding_pct"] is None
    assert row["long_funding_interval_hours"] is None
    assert row["short_funding_pct"] == .02
    html = server.render_funding_pair(row)
    assert '<strong data-live-funding>—</strong>' in html


def test_stream_carry_and_leg_labels_share_one_snapshot_and_expire_together(monkeypatch, live_pair):
    row, rates = live_pair
    monkeypatch.setattr(server, "api_market_spreads", lambda *a: {"groups": [{"routes": [row]}]})
    monkeypatch.setattr(server.api_spreads, "live_route_updates_for", lambda *a, **kw: {
        "A": (1.0, 99.0, 123, "top_book"),
    })
    query = {"funding_only": ["1"]}
    current = server._board_stream_rows(Path("board.json"), query)["A"]
    assert current[:3] == (1.0, pytest.approx(2.04), "top_book")
    assert current[3]["long"] == {"rate_pct": -.08, "cadence": "every 1h"}
    assert current[3]["short"] == {"rate_pct": .02, "cadence": "every 4h"}
    assert current[3]["age_label"] == "Live now · funding 2 min old"
    assert row["long_funding_pct"] == 99  # Shared input was not mutated.
    rates.clear()
    expired = server._board_stream_rows(Path("board.json"), query)["A"]
    assert expired[1] is None
    assert expired[3]["long"]["rate_pct"] is None
    assert expired[3]["age_label"] == "Current funding unavailable"


def test_browser_updates_net_legs_and_group_without_touching_another_pair(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute the browser event-handler regression")
    script = server.render_board_stream_script({"funding_only": ["1"]})
    script = script.split("<script>", 1)[1].split("</script>", 1)[0]
    # A tiny DOM surface models selectors and ownership, not funding math.
    harness = r'''
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(process.argv[2], 'utf8'));
const textNode = value => ({textContent:value, classList:{remove(){},add(){}}, offsetWidth:0});
const scope = () => ({
  nodes:{'[data-live-funding]':[textNode('+99.000%')],
    '[data-live-funding-leg="long"]':[textNode('old long')],
    '[data-live-funding-leg="short"]':[textNode('old short')],
    '[data-live-funding-cadence]':[textNode('every 8h')],
    '[data-live-funding-basis]':[textNode('funding unavailable')],
    '[data-live-funding-age]':[textNode('old age')]},
  querySelectorAll(selector){return this.nodes[selector] || []}, matches(){return false}
});
const summary=scope(), child=scope(), unrelated=scope();
const group={matches(){return true}, querySelector(){return summary}};
const document={querySelector(){return null},addEventListener(){},
  querySelectorAll(selector){
    if(!selector.includes('"A"')) return [unrelated];
    return [group,child];
  }};
let board;
class EventSource{addEventListener(name,handler){if(name==='board')board=handler}close(){}}
vm.runInNewContext(input.script,{window:{EventSource,addEventListener(){}},EventSource,document});
const read=s=>Object.fromEntries(Object.entries(s.nodes).map(([k,v])=>[k,v[0].textContent]));
board({data:JSON.stringify({routes:[{route_key:'A',funding_pct:2.04,funding_legs:{
  long:{rate_pct:-.08,cadence:'every 1h'},short:{rate_pct:.02,cadence:'every 4h'},
  cadence:'every 1h · every 4h',age_label:'Live now · funding 2 min old'}}]})});
const fresh={summary:read(summary),child:read(child),unrelated:read(unrelated)};
board({data:JSON.stringify({routes:[{route_key:'A',funding_pct:null,funding_legs:{
  long:{rate_pct:null,cadence:'schedule unavailable'},short:{rate_pct:null,cadence:'schedule unavailable'},
  cadence:'schedule unavailable',age_label:'Current funding unavailable'}}]})});
console.log(JSON.stringify({fresh,expired:read(child)}));
'''
    js = tmp_path / "stream.cjs"
    data = tmp_path / "input.json"
    js.write_text(harness)
    data.write_text(json.dumps({"script": script}))
    result = subprocess.run([node, str(js), str(data)], check=True, text=True, capture_output=True)
    actual = json.loads(result.stdout)
    for surface in ("summary", "child"):
        row = actual["fresh"][surface]
        assert row["[data-live-funding]"] == "+2.040%"
        assert row['[data-live-funding-leg="long"]'] == "-0.0800% · every 1h"
        assert row['[data-live-funding-leg="short"]'] == "+0.0200% · every 4h"
        assert row["[data-live-funding-cadence]"] == "every 1h · every 4h"
    assert actual["fresh"]["unrelated"]["[data-live-funding]"] == "+99.000%"
    assert actual["expired"]["[data-live-funding]"] == "—"
    assert actual["fresh"]["child"]["[data-live-funding-basis]"] == "24h at current rate"
    assert actual["expired"]["[data-live-funding-basis]"] == "funding unavailable"
    assert actual["expired"]["[data-live-funding-age]"] == "Current funding unavailable"
