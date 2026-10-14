"""Non-sensitive live canary. No business writes, mail access or customizations."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import argparse,json,sys,time
from pathlib import Path
import win11_model_router as r

def main():
    p=argparse.ArgumentParser();p.add_argument('provider',choices=['deepseek','mimo','kimi','claude','gpt']);p.add_argument('--image');p.add_argument('--agent-transport',action='store_true');a=p.parse_args()
    policy=r.load_policy();kind='vision' if a.image else 'text';entry=next(x for x in policy['payload']['routing'][kind+'_chain'] if x.startswith(a.provider+':'))
    job=r.HOME/'probes'/(str(int(time.time()*1000))+'-'+a.provider)
    def accept(text):
        obj=r.parse_json(text)
        if obj!={'ok':True,'number':7}:raise ValueError('probe_invalid')
        return obj
    transport=None
    if a.agent_transport:
        # Use the real agent client on an isolated prompt; no business operation is requested.
        def transport(provider,model,cap):return r.cli_call(provider,model,'Return exactly {"ok":true,"number":7}. Do not use tools.',(),policy['payload'],cap,job,job,True)
    try:
        _,_,receipt=r.complete('Return exactly {"ok":true,"number":7}. Do not use tools.',purpose='non_sensitive_canary',job=job,validate=accept,images=[Path(a.image)] if a.image else (),timeout=150,policy=policy,chain=[entry],use_health=False,transport=transport)
        result={'status':'ok','provider':a.provider,'model':receipt['model'],'actual_model':receipt['actual_model'],'receipt':str(job/'route-receipt.json')}
    except r.Exhausted as exc:result={'status':'failed','provider':a.provider,'outcome':exc.receipt['outcome'],'attempts':exc.receipt['attempts']}
    print(r.canonical(result))
if __name__=='__main__':main()
