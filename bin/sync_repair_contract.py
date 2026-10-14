"""Fixed verifier: immutable oracle, bounded syntax, no candidate-defined tests."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import ast
import itertools
import json
import sys
from pathlib import Path
FUNCTIONS={'email_source','model_error','attachment_complete'}
BUILTINS={'any':any,'all':all}

def load(text):
    if len(text.encode())>12000:raise ValueError('candidate_size')
    tree=ast.parse(text)
    if len(list(ast.walk(tree)))>900:raise ValueError('candidate_complexity')
    if {n.name for n in tree.body if isinstance(n,ast.FunctionDef)}!=FUNCTIONS:raise ValueError('candidate_scope')
    permitted=(ast.Module,ast.Expr,ast.Constant,ast.FunctionDef,ast.arguments,ast.arg,ast.Return,ast.Assign,ast.Name,ast.Load,ast.Store,ast.If,ast.BoolOp,ast.And,ast.Or,ast.UnaryOp,ast.Not,ast.Compare,ast.Eq,ast.NotEq,ast.Is,ast.IsNot,ast.In,ast.NotIn,ast.List,ast.Tuple,ast.Dict,ast.ListComp,ast.GeneratorExp,ast.comprehension,ast.Call,ast.Attribute,ast.Subscript,ast.IfExp)
    for n in ast.walk(tree):
        if not isinstance(n,permitted):raise ValueError('candidate_syntax')
        if isinstance(n,ast.Name) and n.id.startswith('_'):raise ValueError('candidate_private_name')
        if isinstance(n,ast.Attribute) and n.attr!='get':raise ValueError('candidate_attribute')
        if isinstance(n,ast.Call) and not (isinstance(n.func,ast.Attribute) and n.func.attr=='get' or isinstance(n.func,ast.Name) and n.func.id in BUILTINS):raise ValueError('candidate_call')
        if isinstance(n,ast.Assign) and any(not isinstance(t,ast.Name) for t in n.targets):raise ValueError('candidate_mutation')
        if isinstance(n,ast.FunctionDef) and (n.decorator_list or n.args.defaults or n.args.kw_defaults):raise ValueError('candidate_definition')
    for n in tree.body:
        if not isinstance(n,ast.FunctionDef) and not (isinstance(n,ast.Expr) and isinstance(n.value,ast.Constant) and isinstance(n.value.value,str)):raise ValueError('candidate_toplevel')
    scope={'__builtins__':BUILTINS}
    exec(compile(tree,'<sync-rules>','exec'),scope)
    return scope

def cases():
    rows=[]
    for ref,kind,system in itertools.product(('fixture','other',None),('source_index','status',None),('email','chat',None)):
        rows.append(('email_source',[{'source_ref':ref,'memory_kind':kind,'source_system':system},'fixture'],ref=='fixture' and kind=='source_index' and system=='email'))
    for verified,status in itertools.product((True,False,None,'true'),('extracted','partial_static','pending',None)):
        rows.append(('attachment_complete',[{'receipt_verified':verified,'status':status}],verified is True and status=='extracted'))
    neutral={'quota':'model_quota_unavailable','auth':'model_auth_unavailable','timeout':'model_timeout','bad_request':'model_unavailable','invalid_output':'model_output_invalid'}
    legacy={'quota':'codex_subscription_limit','auth':'codex_subscription_auth','timeout':'codex_timeout','bad_request':'codex_model_unavailable','invalid_output':'model_output_invalid'}
    for category in (*neutral,'unknown'):
        for provider in ('deepseek','kimi','gpt'):
            rows.append(('model_error',[category,[{'provider':provider,'category':category}]],(legacy if provider=='gpt' else neutral).get(category,'model_chain_exhausted')))
        rows.append(('model_error',[category,[{'provider':'kimi','category':'invalid_output','rejection_code':'proposal_source_drift'},{'provider':'deepseek','category':'quota'}]],'model_source_changed'))
    rows.append(('model_error',['quota',[{'provider':'gpt','category':'cooling_down'}]],'model_quota_unavailable'))
    return rows

def check(text):
    funcs=load(text);failures=[]
    for index,(name,args,expected) in enumerate(cases()):
        try: actual=funcs[name](*args)
        except Exception: actual='<raised>'
        if type(actual) is not type(expected) or actual!=expected:failures.append(index)
    return {'passed':not failures,'case_count':len(cases()),'failed_cases':failures}
if __name__=='__main__':
    try: result=check(Path(sys.argv[1]).read_text())
    except Exception as e: result={'passed':False,'error':type(e).__name__}
    print(json.dumps(result));raise SystemExit(0 if result['passed'] else 2)
