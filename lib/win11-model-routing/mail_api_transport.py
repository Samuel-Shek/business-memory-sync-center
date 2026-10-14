"""Candidate only: verified HTTPS with the existing three-tool mail host."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import os,sys,json,ssl,subprocess,threading,queue,time,urllib.request,urllib.error,socket,http.client
from pathlib import Path
import win11_model_router as r
TOOLS=('read_chunk','save_draft','submit_candidate')
ENDPOINTS={'mimo':'https://token-plan-cn.xiaomimimo.com/anthropic/v1/messages','deepseek':'https://api.deepseek.com/anthropic/v1/messages','kimi':'https://api.kimi.com/coding/v1/messages'}
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None

def request(provider,body,timeout):
 context=ssl.create_default_context()
 if not context.check_hostname or context.verify_mode!=ssl.CERT_REQUIRED:raise r.Failure('client_integrity')
 req=urllib.request.Request(ENDPOINTS[provider],data=r.canonical(body).encode(),headers={('Authorization' if provider=='mimo' else 'x-api-key'):('Bearer ' if provider=='mimo' else '')+r.key(provider),'anthropic-version':'2023-06-01','Content-Type':'application/json'})
 try:
  with urllib.request.build_opener(NoRedirect,urllib.request.HTTPSHandler(context=context)).open(req,timeout=timeout) as response:
   raw=response.read(8*1024*1024+1)
   if len(raw)>8*1024*1024:raise r.Failure('invalid_output')
   return json.loads(raw)
 except urllib.error.HTTPError as exc:
  # Do not retain provider bodies: they can echo content or credentials.
  raise r.Failure(r.classify('',exc.code)) from None
 except urllib.error.URLError as exc:
  if isinstance(exc.reason,ssl.SSLCertVerificationError):raise r.Failure('tls_certificate',{'reason':'certificate_verification_failed'}) from None
  if isinstance(exc.reason,(TimeoutError,socket.timeout)):raise r.Failure('timeout') from None
  raise r.Failure('network') from None
 except (TimeoutError,socket.timeout):raise r.Failure('timeout') from None
 except (ValueError,UnicodeError):raise r.Failure('invalid_output') from None
 except (OSError,http.client.HTTPException):raise r.Failure('network') from None

def bounded_request(provider,body,timeout):
 try:
  child=subprocess.Popen([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--request-worker',provider,str(timeout)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=r.environment(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
 except OSError:raise r.Failure('unconfigured') from None
 try:
  raw,_=child.communicate(r.canonical(body).encode(),timeout=max(.01,timeout))
 except subprocess.TimeoutExpired:
  # This child only performs the model request; it has no mail tools.
  child.kill()
  try:child.communicate(timeout=5)
  except subprocess.TimeoutExpired:raise r.Uncertain('request_worker_outcome_unknown') from None
  raise r.Failure('timeout') from None
 if child.returncode or len(raw)>8*1024*1024:raise r.Failure('network')
 try:result=json.loads(raw)
 except (ValueError,UnicodeError):raise r.Failure('invalid_output') from None
 if result.get('status')=='failed':raise r.Failure(result['category'],result.get('diagnostic'))
 if result.get('status')!='ok':raise r.Failure('invalid_output')
 return result['response']

class ClosedHost:
 def __init__(self,config,deadline,on_start=None):
  servers=json.loads(Path(config).read_text('utf-8'))['mcpServers']
  if set(servers)!={'mail'}:raise r.Failure('client_integrity')
  spec=servers['mail'];self.deadline=deadline;self.seq=0;self.lines=queue.Queue()
  self.process=subprocess.Popen([spec['command'],*spec['args']],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=r.environment(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
  if on_start:on_start(self.process.pid)
  def read():
   for raw in self.process.stdout:self.lines.put(raw)
   self.lines.put(None)
  threading.Thread(target=read,daemon=True).start()
 def rpc(self,method,params):
  self.seq+=1
  try:
   raw=(r.canonical({'jsonrpc':'2.0','id':self.seq,'method':method,'params':params})+'\n').encode()
   self.process.stdin.write(raw);self.process.stdin.flush()
   line=self.lines.get(timeout=max(.01,min(30,self.deadline-time.monotonic())))
   if not line or len(line)>100*1024:raise ValueError('response')
   result=json.loads(line)
   if result.get('id')!=self.seq or 'error' in result:raise ValueError('response')
   return result['result']
  except (OSError,ValueError,KeyError,queue.Empty):
   # A dispatched write with an unknown result cannot enter model fallback.
   raise r.Uncertain('mail_tool_outcome_unknown_no_replay') from None
 def close(self):
  if self.process.poll() is None:
   self.process.stdin.close()
   try:self.process.wait(timeout=5)
   except subprocess.TimeoutExpired:
    self.process.kill();self.process.wait(timeout=5)


def agent_call(provider,model,prompt,cfg,timeout,cwd,attempt_dir,on_start=None,mcp_config=None,post=bounded_request,host_factory=ClosedHost):
 if provider not in ('mimo','kimi') or provider not in cfg['routing']['enabled_providers']:raise r.Failure('provider_disabled')
 deadline=time.monotonic()+timeout;host=host_factory(mcp_config,deadline,on_start);calls=0;requests=0;usage=[]
 try:
  host.rpc('initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'mail-router-candidate','version':'1'}})
  tools=host.rpc('tools/list',{})['tools']
  if sorted(x['name'] for x in tools)!=sorted(TOOLS):raise r.Failure('client_integrity')
  schemas={x['name']:x['inputSchema'] for x in tools}
  api_tools=[{'name':'mcp__mail__'+x['name'],'description':x['description'],'input_schema':x['inputSchema']} for x in tools]
  from datetime import datetime,timezone
  finish=datetime.fromtimestamp(time.time()+max(0,timeout-90),timezone.utc).isoformat()
  prompt+='\nTrusted workflow deadline UTC '+finish+'. Finish and return final JSON before the deadline; if time is short, save reviewed-part drafts and keep incomplete items pending. Review at most five assigned items; never claim unread attachments complete.'
  messages=[{'role':'user','content':prompt}]
  while time.monotonic()<deadline:
   body={'model':model,'max_tokens':cfg['routing']['max_output_tokens'],'stream':False,'messages':messages,'tools':api_tools,'output_config':{'effort':'max' if provider=='kimi' else 'high'},'thinking':{'type':'enabled' if provider=='mimo' else 'adaptive'}}
   response=post(provider,body,max(.01,min(cfg['routing']['per_attempt_timeout_seconds'],deadline-time.monotonic())));requests+=1
   actual=response.get('model',model)
   if actual!=model and not str(actual).startswith(model+'-'):raise r.Failure('invalid_output',{'reason':'model_identity_mismatch'})
   content=response.get('content');stop=response.get('stop_reason')
   if not isinstance(content,list) or not content:raise r.Failure('empty')
   usage.append(response.get('usage',{}));invocations=[x for x in content if x.get('type')=='tool_use']
   if invocations:
    if stop!='tool_use':raise r.Failure('invalid_output')
    # Validate every name before dispatching any call in a batch.
    ids=set()
    for x in invocations:
     if x.get('name') not in {'mcp__mail__'+n for n in TOOLS} or not isinstance(x.get('input'),dict):raise r.Failure('invalid_output',{'reason':'unauthorized_tool'})
     if not isinstance(x.get('id'),str) or not x['id'] or len(x['id'])>256 or x['id'] in ids:raise r.Failure('invalid_output',{'reason':'invalid_tool_id'})
     ids.add(x['id']);schema=schemas[x['name'].removeprefix('mcp__mail__')];args=x['input'];properties=schema.get('properties',{})
     if set(args)-set(properties) or set(schema.get('required',[]))-set(args):raise r.Failure('invalid_output',{'reason':'invalid_tool_arguments'})
     for name,value in args.items():
      kind=properties[name]['type']
      if kind=='string' and not isinstance(value,str) or kind=='integer' and type(value)!=int:raise r.Failure('invalid_output',{'reason':'invalid_tool_arguments'})
    messages.append({'role':'assistant','content':content});results=[]
    for x in invocations:
     if time.monotonic()>=deadline:raise r.Failure('timeout')
     result=host.rpc('tools/call',{'name':x['name'].removeprefix('mcp__mail__'),'arguments':x['input']});calls+=1
     results.append({'type':'tool_result','tool_use_id':x['id'],'content':result.get('content',[]),'is_error':bool(result.get('isError'))})
    messages.append({'role':'user','content':results});continue
   if stop!='end_turn':raise r.Failure('invalid_output')
   text='\n'.join(x['text'] for x in content if x.get('type')=='text')
   if not text.strip():raise r.Failure('empty')
   return text,{'actual_model':actual,'transport':'anthropic_https_closed_mail_host','requests':requests,'tool_calls':calls,'usage':usage,'tls_verification':'required','effort':'max' if provider=='kimi' else 'high'}
  raise r.Failure('timeout')
 finally:host.close()

if __name__=='__main__':
 if len(sys.argv)!=4 or sys.argv[1]!='--request-worker' or sys.argv[2] not in ('mimo','kimi'):sys.exit(2)
 try:
  raw=sys.stdin.buffer.read(32*1024*1024+1)
  if len(raw)>32*1024*1024:raise r.Failure('invalid_output')
  if os.environ.get('BUSINESS_MIMO_CREDENTIAL_PROBE'):
   if Path(__file__).resolve().parent.parent.name!='staging' or os.environ['BUSINESS_MIMO_CREDENTIAL_PROBE']!='mimo-candidate-20261003.bin':raise r.Failure('client_integrity')
   real_key=r.key;r.key=lambda p:r.protect((r.HOME/'credentials/mimo-candidate-20261003.bin').read_bytes(),True).decode() if p=='mimo' else real_key(p)
  response=request(sys.argv[2],json.loads(raw),float(sys.argv[3]));out={'status':'ok','response':response}
 except r.Failure as e:out={'status':'failed','category':e.category,'diagnostic':e.diagnostic}
 except Exception:out={'status':'failed','category':'network'}
 sys.stdout.buffer.write(r.canonical(out).encode());sys.stdout.buffer.flush()
