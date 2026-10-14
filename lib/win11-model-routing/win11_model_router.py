"""Windows native governed routing. Keys are DPAPI protected; no network service."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import base64, contextlib, ctypes, hashlib, http.client, json, mimetypes, os, re, socket, ssl, subprocess, sys, time, urllib.error, urllib.request, uuid
from pathlib import Path

HOME = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'BusinessMemory' / 'model-router'
POLICY = Path(__file__).with_name('routing.json')
CODEX = Path.home()/'AppData/Local/Programs/OpenAI/Codex/bin/codex.exe'
CLAUDE = Path.home()/'.local/bin/claude.exe'
DISABLED = ('apps','plugins','remote_plugin','plugin_sharing','skill_search','skill_mcp_dependency_install','hooks','memories','multi_agent','multi_agent_v2','browser_use','browser_use_external','browser_use_full_cdp_access','computer_use','in_app_browser','image_generation','in_app_local_automation','goals','sleep_tool')

def canonical(x): return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',',':'))
def digest(x): return hashlib.sha256(x).hexdigest()
def atomic(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temp.open('wb') as f: f.write((canonical(value)+'\n').encode());f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
def load_policy(path=POLICY):
    obj=json.loads(Path(path).read_text('utf-8'))
    if obj['sha256'] != digest(canonical(obj['payload']).encode()): raise ValueError('routing_policy_hash_mismatch')
    return obj

@contextlib.contextmanager
def locked(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as f:
        f.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try: yield
        finally:
            f.seek(0)
            if os.name=='nt': msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(f,fcntl.LOCK_UN)

class Blob(ctypes.Structure):
    _fields_=[('size',ctypes.c_ulong),('data',ctypes.POINTER(ctypes.c_ubyte))]
def protect(data, decrypt=False):
    if os.name!='nt': raise ValueError('windows_required')
    buf=ctypes.create_string_buffer(data); inp=Blob(len(data),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)));out=Blob()
    crypt=ctypes.windll.crypt32
    ok=(crypt.CryptUnprotectData(ctypes.byref(inp),None,None,None,None,1,ctypes.byref(out)) if decrypt else crypt.CryptProtectData(ctypes.byref(inp),'BusinessMemory model routing',None,None,None,1,ctypes.byref(out)))
    if not ok: raise ValueError('credential_protection_failed')
    try: return ctypes.string_at(out.data,out.size)
    finally: ctypes.windll.kernel32.LocalFree(out.data)
def key(provider):
    p=HOME/'credentials'/(provider+'.bin')
    if not p.is_file() or p.is_symlink(): raise Failure('unconfigured')
    try: return protect(p.read_bytes(),True).decode().strip()
    except Exception: raise Failure('auth') from None

def environment():
    prefixes=('ANTHROPIC_','CLAUDE_API_','CLAUDE_BASE_','OPENAI_','AZURE_OPENAI_')
    names={'CODEX_API_KEY','MIMO_API_KEY','KIMI_API_KEY','DEEPSEEK_API_KEY','CLAUDE_CODE_USE_BEDROCK','CLAUDE_CODE_USE_VERTEX','CLAUDE_CODE_USE_FOUNDRY','CLAUDE_CODE_OAUTH_TOKEN','CLAUDE_CODE_SUBAGENT_MODEL','AWS_BEARER_TOKEN_BEDROCK'}
    return {k:v for k,v in os.environ.items() if not k.upper().startswith(prefixes) and k.upper() not in names}
class Failure(Exception):
    def __init__(self, category, diagnostic=None):
        self.category=category;self.diagnostic=diagnostic or {};super().__init__(category)

class OutputInvalid(ValueError):
    def __init__(self, reason, field=None):
        self.diagnostic = {"reason": reason}
        if field is not None: self.diagnostic["field"] = field
        super().__init__(reason)

def output_diagnostic(stage, text, reason, field=None):
    result = {"stage": stage, "reason": reason, "output_bytes": len(text.encode('utf-8')),
              "output_sha256": digest(text.encode('utf-8'))}
    if field is not None: result["field"] = field
    return result
class Uncertain(Exception): pass
class Exhausted(Exception):
    def __init__(self,receipt): self.receipt=receipt;super().__init__(receipt['outcome'])
def classify(text,status=0):
    t=text.lower()
    if any(x in t for x in ('unknown_certificate_verification_error','certificate_verify_failed','certificate verification','certificate verify','unable to verify the first certificate','unable_to_verify_leaf_signature','self_signed_cert','cert_has_expired')):return 'tls_certificate'
    if status==429 or any(x in t for x in ('quota','rate limit','usage limit','at capacity','overloaded','insufficient_quota')): return 'quota'
    if status in (401,403) or any(x in t for x in ('not logged in','unauthorized','authentication','invalid api key')): return 'auth'
    if 'timeout' in t or 'timed out' in t:return 'timeout'
    if any(x in t for x in ('connection','network','socket','stream disconnected')):return 'network'
    return 'server' if status>=500 or not status else 'bad_request'
def failure_from_text(text,status=0):
    category=classify(text,status)
    return Failure(category,{'reason':'certificate_verification_failed'} if category=='tls_certificate' else None)

def parse_json(text):
    text=text.strip()
    if text.startswith('```'):
        lines=text.splitlines();text='\n'.join(lines[1:-1])
    return json.loads(text)

def run_process(command,stdin,env,cwd,timeout,on_start=None):
    try:
        p=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,cwd=cwd,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except OSError: raise Failure('unconfigured') from None
    if on_start:on_start(p.pid)
    try:out,err=p.communicate(stdin.encode('utf-8'),timeout=timeout)
    except subprocess.TimeoutExpired:
        # Only this invocation's process tree. Never terminate an unrelated runner.
        if os.name=='nt':
            killed=subprocess.run(['taskkill','/PID',str(p.pid),'/T','/F'],capture_output=True,timeout=15)
            if killed.returncode and p.poll() is None: raise Uncertain('model_process_outcome_unknown')
        else: p.kill()
        try:p.communicate(timeout=10)
        except subprocess.TimeoutExpired:raise Uncertain('model_process_outcome_unknown')
        raise Failure('timeout') from None
    out,err=out.decode('utf-8','replace'),err.decode('utf-8','replace')
    if p.returncode:raise failure_from_text(err+'\n'+out)
    return out

def http_call(provider,model,prompt,images,cfg,timeout):
    if provider not in cfg['routing'].get('enabled_providers',['deepseek','kimi']):raise Failure('provider_disabled')
    spec=cfg['providers'][provider];content=prompt
    if images:
        content=[{'type':'text','text':prompt}]+[{'type':'image_url','image_url':{'url':'data:'+(mimetypes.guess_type(str(p))[0] or 'image/png')+';base64,'+base64.b64encode(Path(p).read_bytes()).decode()}} for p in images]
    body=dict(model=model,messages=[dict(role='user',content=content)],max_tokens=cfg['routing']['max_output_tokens'],**spec['options'])
    req=urllib.request.Request(spec['base_url']+'/chat/completions',data=canonical(body).encode(),headers={'Authorization':'Bearer '+key(provider),'Content-Type':'application/json'})
    # Do not forward credentials on redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*a,**kw):return None
    try:
        with urllib.request.build_opener(NoRedirect).open(req,timeout=timeout) as r:raw=json.loads(r.read())
    except urllib.error.HTTPError as e:raise Failure(classify(e.read().decode('utf-8','replace'),e.code)) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason,ssl.SSLCertVerificationError):raise Failure('tls_certificate',{'reason':'certificate_verification_failed'}) from None
        if isinstance(exc.reason,(TimeoutError,socket.timeout)):raise Failure('timeout') from None
        raise Failure('network') from None
    except (TimeoutError,socket.timeout):raise Failure('timeout') from None
    except (OSError,http.client.HTTPException):raise Failure('network') from None
    except (ValueError,UnicodeError):raise Failure('server') from None
    try:
        c=raw['choices'][0];text=c['message']['content'];actual=raw.get('model',model)
        if c.get('finish_reason')=='length' or not isinstance(text,str) or not text.strip():raise Failure('empty')
    except (KeyError,TypeError,IndexError):raise Failure('server') from None
    # Accept a provider's dated revision of the requested model, never another family.
    if actual!=model and not str(actual).startswith(model+'-'):raise Failure('invalid_output')
    return text,{'actual_model':actual,'transport':'http','response_id':raw.get('id'),'usage':raw.get('usage'),'tool_calls':0}

def cli_call(provider,model,prompt,images,cfg,timeout,cwd,attempt_dir,agent=False,on_start=None,output_schema=None,mcp_config=None):
    if provider not in cfg['routing'].get('enabled_providers',['deepseek','kimi']):raise Failure('provider_disabled')
    if agent and mcp_config is not None and provider in ('mimo','kimi'):
        if images:raise Failure('invalid_output',{'reason':'mail_transport_images_not_supported'})
        from mail_api_transport import agent_call
        return agent_call(provider,model,prompt,cfg,timeout,cwd,attempt_dir,on_start=on_start,mcp_config=mcp_config)
    env=environment();effort='max' if provider=='kimi' else 'high'
    request_timeout=int(cfg['routing']['per_attempt_timeout_seconds'])
    env['API_TIMEOUT_MS']=str(request_timeout*1000)
    env['CLAUDE_CODE_EFFORT_LEVEL']=effort
    if agent:
        from datetime import datetime, timezone
        finish=datetime.fromtimestamp(time.time()+max(0,timeout-90),timezone.utc).isoformat()
        prompt+='\n\n可信运行时预算：这是多次模型请求组成的整批流程，单次请求上限为 '+str(request_timeout)+' 秒。请在 UTC '+finish+' 前结束本次处理并交付完整的最终 JSON。时间不足时保存已提炼的分段草稿，保持未完成项目 pending，准确报告剩余进度；不得标记未读附件完整，也不得声称整批已完成。切换接管后先核对既有回执与草稿，同一轮合计最多审阅五个项目。'
    if provider in ('claude','kimi') or provider in ('mimo','deepseek') and agent:
        command=[str(CLAUDE),'-p','--safe-mode','--setting-sources','','--disable-slash-commands','--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--no-chrome','--no-session-persistence','--model',model,'--effort',effort,'--output-format','json']
        if output_schema is not None:
            command+=['--json-schema',canonical(output_schema)]
            env['MAX_STRUCTURED_OUTPUT_RETRIES']='0'
        if agent and mcp_config is not None:
            command[command.index('--safe-mode')]='--bare'
            env.pop('CLAUDE_CODE_SAFE_MODE',None)
            command[command.index('--mcp-config')+1]=str(mcp_config)
            command+=['--tools','','--allowedTools','mcp__mail__read_chunk,mcp__mail__save_draft,mcp__mail__submit_candidate','--permission-mode','dontAsk']
        elif agent:command+=['--tools','Bash,Read,Write,Edit,Glob,Grep','--permission-mode','bypassPermissions']
        else:command+=['--tools','','--max-turns','1']
        if provider=='kimi':
            env.update(ANTHROPIC_BASE_URL='https://api.kimi.com/coding/',ANTHROPIC_API_KEY=key('kimi'),ANTHROPIC_AUTH_TOKEN='',CLAUDE_CODE_EFFORT_LEVEL='max')
            for name in ('ANTHROPIC_MODEL','ANTHROPIC_DEFAULT_OPUS_MODEL','ANTHROPIC_DEFAULT_SONNET_MODEL','ANTHROPIC_DEFAULT_HAIKU_MODEL','CLAUDE_CODE_SUBAGENT_MODEL'):env[name]=model
        if provider=='mimo':
            env.update(ANTHROPIC_BASE_URL='https://token-plan-cn.xiaomimimo.com/anthropic',ANTHROPIC_API_KEY='',ANTHROPIC_AUTH_TOKEN=key('mimo'),CLAUDE_CODE_EFFORT_LEVEL='high')
            for name in ('ANTHROPIC_MODEL','ANTHROPIC_DEFAULT_OPUS_MODEL','ANTHROPIC_DEFAULT_SONNET_MODEL','ANTHROPIC_DEFAULT_HAIKU_MODEL','CLAUDE_CODE_SUBAGENT_MODEL'):env[name]=model
        if provider=='deepseek':
            # DeepSeek Anthropic-compatible endpoint; x-api-key fully supported.
            env.update(ANTHROPIC_BASE_URL='https://api.deepseek.com/anthropic',ANTHROPIC_API_KEY=key('deepseek'),ANTHROPIC_AUTH_TOKEN='',CLAUDE_CODE_EFFORT_LEVEL='high')
            for name in ('ANTHROPIC_MODEL','ANTHROPIC_DEFAULT_OPUS_MODEL','ANTHROPIC_DEFAULT_SONNET_MODEL','ANTHROPIC_DEFAULT_HAIKU_MODEL','CLAUDE_CODE_SUBAGENT_MODEL'):env[name]=model
        stdin=prompt
        if images:
            command+=['--input-format','stream-json']
            content=[dict(type='text',text=prompt)]+[dict(type='image',source=dict(type='base64',media_type=mimetypes.guess_type(str(p))[0] or 'image/png',data=base64.b64encode(Path(p).read_bytes()).decode())) for p in images]
            stdin=canonical({'type':'user','message':{'role':'user','content':content}})+'\n'
        out=run_process(command,stdin,env,cwd,timeout,on_start)
        try:result=json.loads(out)
        except ValueError:raise Failure('invalid_output', output_diagnostic('example_24b073193a85', out, 'json_invalid')) from None
        if result.get('is_error'):raise failure_from_text(str(result.get('result','')))
        if output_schema is not None:
            structured=result.get('structured_output')
            if not isinstance(structured,dict):
                raise Failure('invalid_output', output_diagnostic('example_e9aed3108bd7', out, 'structured_output_missing'))
            text=canonical(structured)
        else:
            text=result.get('result','')
        used=list((result.get('modelUsage') or {}).keys())
        matched=[m for m in used if m==model or m.startswith(model+'-')]
        if used and not matched:raise Failure('invalid_output', output_diagnostic('model_identity', out, 'requested_model_not_reported'))
        if not text:raise Failure('empty')
        return text,{'actual_model':matched[0] if matched else model,'auxiliary_models':[m for m in used if m not in matched],'transport':'claude_cli','usage':result.get('usage'),'tool_calls':None if agent else 0,'exit_code':0}
    result_path=attempt_dir/'last-message.txt'
    command=[str(CODEX),'exec','--ephemeral','--ignore-user-config','--ignore-rules','--json','--skip-git-repo-check','-m',model,'-c','model_reasoning_effort="high"','-c','web_search="disabled"','-c','approval_policy="never"','-s','danger-full-access' if agent else 'read-only','-C',str(cwd),'--output-last-message',str(result_path)]
    # Native OpenAI provider is reserved and already uses the same defaults as Mac.
    if provider=='mimo':command+=['-c','model_providers.mimo.stream_idle_timeout_ms='+str(request_timeout*1000)]
    for feature in DISABLED:command+=['--disable',feature]
    if not agent:
        for feature in ('shell_tool','unified_exec','code_mode_host','workspace_dependencies'):command+=['--disable',feature]
    if provider=='mimo':
        env['MIMO_API_KEY']=key('mimo')
        for setting in ('model_provider="mimo"','model_providers.mimo.name="MiMo Token Plan"','model_providers.mimo.base_url="https://token-plan-cn.xiaomimimo.com/v1"','model_providers.mimo.env_key="MIMO_API_KEY"','model_providers.mimo.wire_api="responses"','model_context_window=1048576'):
            command+=['-c',setting]
    for image in images:command+=['--image',str(image)]
    command+=['-']
    out=run_process(command,prompt,env,cwd,timeout,on_start)
    events=[]
    for line in out.splitlines():
        try:events.append(json.loads(line))
        except ValueError:pass
    if not any(e.get('type')=='turn.completed' for e in events):raise Failure('invalid_output')
    if any(e.get('type') in ('error','turn.failed') for e in events):raise Failure(classify(out))
    if not agent and any(e.get('item',{}).get('type') in ('command_execution','mcp_tool_call','web_search','file_change') for e in events):raise Failure('invalid_output')
    if not result_path.is_file():raise Failure('empty')
    text=result_path.read_text('utf-8')
    return text,{'actual_model':model,'transport':'codex_cli','exit_code':0,'completion_evidence':'turn.completed','tool_calls':None if agent else 0}

def complete(prompt,*,purpose,job,validate,images=(),timeout=900,agent=False,cwd=None,policy=None,transport=None,checkpoint=None,chain=None,use_health=True,output_schema=None,mcp_config=None):
    policy=policy or load_policy();cfg=policy['payload'];route=cfg['routing'];job=Path(job);job.mkdir(parents=True,exist_ok=True)
    kind='vision' if images else 'text'
    enabled=route.get('enabled_providers',['deepseek','kimi'])
    if not isinstance(enabled,list) or not enabled or any(p not in cfg['providers'] for p in enabled):raise ValueError('enabled_providers_invalid')
    base=[e for e in route[kind+'_chain'] if e.split(':',1)[0] in enabled]
    order=[e for e in (chain or base) if e.split(':',1)[0] in enabled] or base
    if not order:raise ValueError('enabled_model_chain_empty')
    # Tests/probes can pin a provider; production callers omit chain.
    implementation_sha=digest(Path(__file__).read_bytes()+(Path(__file__).with_name('mail_api_transport.py').read_bytes() if agent and mcp_config is not None else b''))
    binding=digest(canonical(dict(implementation_sha256=implementation_sha,prompt=prompt,images=[digest(Path(p).read_bytes()) for p in images],policy=policy['sha256'],purpose=purpose,agent=agent,order=order)).encode())
    if output_schema is not None: binding=digest((binding+canonical(output_schema)).encode())
    if mcp_config is not None: binding=digest((binding+digest(Path(mcp_config).read_bytes())).encode())
    with locked(job/'route.lock'):
        journal=job/'route-receipt.json'
        state=json.loads(journal.read_text('utf-8')) if journal.exists() else dict(schema_version=2,binding=binding,implementation_sha256=implementation_sha,policy_sha256=policy['sha256'],routing_version=policy['version'],purpose=purpose,attempts=[],outcome='running')
        if state['binding']!=binding:raise Uncertain('model_input_changed')
        if state['outcome']=='ok':
            text=(job/'result.txt').read_text('utf-8')
            if digest(text.encode())!=state['result_sha256']:raise Uncertain('model_result_hash_mismatch')
            return text,validate(text),state
        if any(a['category']=='started' for a in state['attempts']):raise Uncertain('model_result_uncertain_no_blind_retry')
        if state['outcome']!='running':raise Exhausted(state)
        deadline=time.monotonic()+timeout
        entries=[(e,1) for e in order]+[(order[0],2)]
        for idx,(entry,cycle) in enumerate(entries):
            if idx<len(state['attempts']):continue
            if time.monotonic()>=deadline:state['outcome']='budget_exhausted';break
            provider,model=entry.split(':',1);remaining=0
            hp=HOME/'health'/((('agent-' if agent else 'summary-')+provider)+'.json')
            if use_health and hp.exists():
                try:remaining=max(0,json.loads(hp.read_text())['cooldown_until']-time.time())
                except (ValueError,KeyError,OSError):pass
            attempt=dict(provider=provider,model=model,cycle=cycle,category='started',started_at=time.time())
            if cycle==1 and remaining:
                attempt.update(category='cooling_down',seconds=0);state['attempts'].append(attempt);atomic(journal,state);continue
            if checkpoint is not None:attempt['checkpoint']=checkpoint()
            state['attempts'].append(attempt);atomic(journal,state)
            ad=job/('attempt-'+str(idx+1));ad.mkdir(exist_ok=True)
            def started(pid):attempt['pid']=pid;atomic(journal,state)
            begun=time.monotonic()
            try:
                remaining_budget=max(1,deadline-time.monotonic())
                # A CLI agent performs MANY model requests plus extraction and disk I/O.
                # Its workflow budget is not the timeout of one model request.
                cap=remaining_budget if agent else min(route['per_attempt_timeout_seconds'],remaining_budget)
                attempt.update(request_timeout_seconds=None if agent and provider=='gpt' else route['per_attempt_timeout_seconds'],request_timeout_control='native_codex_defaults' if agent and provider=='gpt' else 'configured',invocation_budget_seconds=round(cap,2),timeout_scope='multi_request_workflow' if agent else 'single_model_call')
                atomic(journal,state)
                if transport:text,evidence=transport(provider,model,cap)
                elif not agent and provider in ('deepseek','mimo','kimi'):text,evidence=http_call(provider,model,prompt,images,cfg,cap)
                else:text,evidence=cli_call(provider,model,prompt,images,cfg,cap,cwd or job,ad,agent,started,output_schema=output_schema,mcp_config=mcp_config)
                try:value=validate(text)
                except OutputInvalid as exc:
                    raise Failure('invalid_output', output_diagnostic('business_result', text, **exc.diagnostic)) from None
                except json.JSONDecodeError:
                    raise Failure('invalid_output', output_diagnostic('business_result', text, 'json_invalid')) from None
                except Exception:
                    raise Failure('invalid_output', output_diagnostic('business_result', text, 'validator_rejected')) from None
            except Failure as exc:
                category='workflow_budget_exhausted' if agent and exc.category=='timeout' and time.monotonic()>=deadline else exc.category
                attempt.update(category=category,seconds=round(time.monotonic()-begun,3))
                if exc.diagnostic: attempt['diagnostic'] = exc.diagnostic
                atomic(journal,state)
                if category in ('tls_certificate','client_integrity'):
                    state['outcome']='client_security_failed'
                    break
                if category=='workflow_budget_exhausted':state['outcome']='budget_exhausted';break
                if use_health:atomic(hp,{'category':category,'cooldown_until':time.time()+route['cooldown_seconds'].get(category,300)})
                continue
            except BaseException:
                # Persisted started marker deliberately prevents unknown-outcome replay.
                raise
            attempt.update(category='ok',seconds=round(time.monotonic()-begun,3),evidence=evidence)
            if use_health:atomic(hp,{'category':'ok','cooldown_until':0})
            temp=job/'result.txt.tmp';temp.write_text(text,encoding='utf-8');os.replace(temp,job/'result.txt')
            state.update(outcome='ok',provider=provider,model=model,actual_model=evidence.get('actual_model',model),result_sha256=digest(text.encode()),completed_at=time.time())
            atomic(journal,state);return text,value,state
        if state['outcome']=='running':state['outcome']='chain_exhausted'
        state['completed_at']=time.time();atomic(journal,state);raise Exhausted(state)
