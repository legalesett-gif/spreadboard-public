"""Execute the production refresh JS: hidden pauses and expired sessions."""
import json
import shutil
import subprocess

import pytest

from spreadboard import server


@pytest.mark.parametrize("scenario", ["silent_pause", "visible_pause", "login", "failure", "resume"])
def test_refresh_behavior(tmp_path, scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for browser handler regression")
    script = server.render_auto_refresh_script().split("<script>")[1].split("</script>")[0]
    harness = r'''
const vm=require('node:vm'), fs=require('node:fs');
const {script,scenario}=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const events={}, notices=[], dispatched=[];let tick, calls=0, replaced=0;
const item=()=>({textContent:'',dataset:{},classList:{toggle(){}},setAttribute(){},
 addEventListener(){},appendChild(child){this.child=child},remove(){this.removed=true},
 querySelector(){return item()}});
const root={dataset:{refresh:'3',refreshSilent:scenario==='visible_pause'?'0':'1'},
 querySelectorAll(){return []}, replaceWith(){replaced++},before(n){notices.push(n)}};
const document={hidden:false,activeElement:null,body:{appendChild(x){notices.push(x)}},
 createElement:item,querySelector(s){return s==='[data-refresh]'?root:null},
 addEventListener(name,f){events[name]=f},dispatchEvent(e){dispatched.push(e.type)}};
const response={ok:scenario!=='failure',status:scenario==='failure'?503:200,
 redirected:scenario==='login',url:'https://spreadarbitrage.ink/login',text:async()=>'<section>'};
const context={document,location:{href:'https://spreadarbitrage.ink/funding?rank=7d',pathname:'/funding',search:'?rank=7d'},
 localStorage:{getItem(){return '1'}},sessionStorage:{getItem(){return null}},
 setInterval(f){tick=f},setTimeout(){return 1},clearTimeout(){},AbortController,
 URL,encodeURIComponent,CustomEvent:class{constructor(type){this.type=type}},
 DOMParser:class{parseFromString(){return {querySelector(){return root}}}},
 fetch:async()=>{calls++;return response}};
vm.runInNewContext(script,context);
(async()=>{
 if(scenario==='resume')await events.visibilitychange();
 else if(['login','failure'].includes(scenario))await events['spreadboard:refresh-now']();
 else {tick();tick();tick()}
 await new Promise(resolve=>setImmediate(resolve));
 if(scenario==='login')await events['spreadboard:refresh-now']();
 console.log(JSON.stringify({calls,replaced,notices:notices.map(n=>n.textContent),dispatched}));
})();
'''
    js = tmp_path / "check.cjs"
    data = tmp_path / "data.json"
    js.write_text(harness)
    data.write_text(json.dumps({"script": script, "scenario": scenario}))
    actual = json.loads(subprocess.run([node, str(js), str(data)], check=True,
                                     capture_output=True, text=True).stdout)
    assert actual["calls"] == (0 if scenario == "visible_pause" else 1)
    if scenario in {"silent_pause", "resume"}:
        assert actual["replaced"] == 1
    if scenario == "login":
        assert actual["replaced"] == 0
        assert "spreadboard:session-expired" in actual["dispatched"]
        assert any("Session expired" in text for text in actual["notices"])
    if scenario == "failure":
        assert any("out of date" in text for text in actual["notices"])


def test_expired_session_closes_live_stream():
    source = server.render_board_stream_script({})
    assert 'document.addEventListener("spreadboard:session-expired"' in source
    assert "if (sessionExpired) return" in source
