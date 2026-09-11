"""The acceptance CLI must reject incomplete or misleading runtime evidence."""
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def test_acceptance_evidence_boundaries():
    summary=Path(__file__).resolve().parents[1]/'scripts'/'summarize_stability_soak.py'
    end=172800
    base=[{'kind':'start','at':0}]
    for at in range(0,end+1,15):
     c={'name':'app','container_id':'same','memory_limit_bytes':3000,'memory_anon_bytes':2000,
        'memory_peak_bytes':2500,'cpu':{'usage_usec':at*10000},'health':'healthy','cgroup_oom_kill':0,'restart_count':0}
     base.append({'kind':'host','at':at,'containers':[{**c, 'name':name, 'container_id':name+'-same'} for name in ('app-app-1','app-collector-1','app-accounting-worker-1')]})
    for at in sorted(set(range(0,3601,120))|set(range(3900,end+1,300))):
     for path in ['/api/health','/free']:
      base.append({'kind':'endpoint','at':at,'requested_at':at,'path':path,'code':200,
                   'universe':{'current_priced_route_count':1000}})
    results={}
    with tempfile.TemporaryDirectory() as tmp:
     def run(name,rows,expected):
      p=Path(tmp)/'samples.jsonl';p.write_text('\n'.join(json.dumps(r) for r in rows))
      result=json.loads(subprocess.check_output([sys.executable,str(summary),str(p)],text=True))
      assert result['runtime_48h_pass'] is expected,(name,result)
      results[name]=result['runtime_48h_pass']
     run('complete_dense_48h',base,True)
     run('host_observation_gap',[r for r in base if not(r['kind']=='host' and 5000<r['at']<5100)],False)
     run('endpoint_gap',[r for r in base if not(r['kind']=='endpoint' and r['path']=='/free' and r['at']==7200)],False)
     changed=copy.deepcopy(base)
     for r in changed:
      if r['kind']=='host' and 5000<r['at']<5120:r['containers'][0]['health']='unhealthy'
     run('unhealthy_over_90s',changed,False)
     changed=copy.deepcopy(base)
     for r in changed:
      if r['kind']=='host' and r['at']>=end-100:r['containers'][0]['cgroup_oom_kill']=1
     run('oom',changed,False)
     changed=copy.deepcopy(base)
     for r in changed:
      if r['kind']=='host' and r['at']>=end-100:r['containers'][0]['memory_limit_bytes']=2900
     run('recent_cap_change',changed,False)
     run('recent_observer_restart',base+[{'kind':'start','at':end-1800}],False)
     changed=copy.deepcopy(base)
     for r in changed:
      if r['kind']=='endpoint':r['universe']['current_priced_route_count']=0
     run('empty_routes',changed,False)
     changed=copy.deepcopy(base)
     changed[1]['containers'][0]['health']='starting'
     run('startup_contamination',changed,False)
     changed=copy.deepcopy(base)
     next(r for r in changed if r['kind']=='host' and r['at']==end)['containers'][0]['health']='unhealthy'
     run('unhealthy_at_completion',changed,False)
     run('not_yet_sampled',[{'kind':'start','at':0}],False)
     for name,field,value in [('missing_anon','memory_anon_bytes',None),
                              ('invalid_peak','memory_peak_bytes',float('nan')),
                              ('observed_oom_later_zero','cgroup_oom_kill',1),
                              ('observed_restart_later_zero','restart_count',1)]:
      changed=copy.deepcopy(base)
      next(r for r in changed if r['kind']=='host' and r['at']==6000)['containers'][0][field]=value
      run(name,changed,False)
     for name,value in [('missing_cpu',None),('cpu_counter_decreased',0)]:
      changed=copy.deepcopy(base)
      next(r for r in changed if r['kind']=='host' and r['at']==6000)['containers'][0]['cpu']['usage_usec']=value
      run(name,changed,False)
     for name,at,field in [('missing_current_identity',end,'container_id'),
                           ('missing_in_window_identity',6000,'container_id'),
                           ('missing_current_limit',end,'memory_limit_bytes'),
                           ('missing_in_window_limit',6000,'memory_limit_bytes')]:
      changed=copy.deepcopy(base)
      next(r for r in changed if r['kind']=='host' and r['at']==at)['containers'][0].pop(field)
      run(name,changed,False)
     run('older_deploy_without_identity',[{'kind':'host','at':-15,'containers':[{'name':'app'}]}]+base,True)
     for field in ('cgroup_oom_kill','restart_count','health'):
      changed=copy.deepcopy(base)
      next(r for r in changed if r['kind']=='host' and r['at']==end)['containers'][0].pop(field)
      run('missing_latest_'+field,changed,False)
     for value in (True,float('nan'),float('inf')):
      changed=copy.deepcopy(base)
      for r in changed:
       if r['kind']=='endpoint':r['universe']['current_priced_route_count']=value
      run('invalid_route_count_'+str(value),changed,False)
     changed=copy.deepcopy(base)
     for r in changed:
      if r['kind']=='host':r['containers'].pop()
     run('missing_required_container',changed,False)
     changed=copy.deepcopy(base)
     for r in changed:
      if r['kind']=='host':
       for c in r['containers']:c['container_id']='same-aliased-container'
     run('duplicate_container_identity',changed,False)
