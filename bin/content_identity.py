"""Rebuildable private source mapping and fenced extraction cache. No business writes."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib, json, os, sqlite3, time, uuid
from pathlib import Path
from contextlib import contextmanager

VERSION = 'cross-source-v1'
RELATIONS = {'bytes_equal','scope_equal','next_version','derived','candidate','branch'}
class AccessDenied(PermissionError): pass
class StaleWork(RuntimeError): pass

def canonical(v): return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
def digest(b): return hashlib.sha256(b).hexdigest()
def key(v): return digest(canonical(v).encode())

def snapshot(path):
    """Freeze bytes in memory, reject symlinks and changes while reading."""
    p=Path(path)
    if p.is_symlink(): raise AccessDenied('snapshot_symlink')
    with p.open('rb') as f:
        a=os.fstat(f.fileno()); data=f.read(); b=os.fstat(f.fileno())
    c=p.stat()
    sig=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    # Windows Python's descriptor ctime and path ctime have different birth/change
    # semantics. Compare ctime within the same API; compare path identity + mtime.
    path_sig=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
    if sig(a)!=sig(b) or path_sig(b)!=path_sig(c) or len(data)!=a.st_size: raise StaleWork('source_changed')
    return {'sha256':digest(data),'bytes':len(data),'path':str(p.resolve())},data

def valid_product(p):
    """Independent coverage checks; legacy success flags alone are insufficient."""
    if not isinstance(p,dict) or not isinstance(p.get('text'),str): return False
    text=p['text']; c=p.get('coverage') or {}; nodes=c.get('nodes')
    if (p.get('text_sha256')!=digest(text.encode()) or p.get('text_bytes')!=len(text.encode())
        or c.get('complete') is not True or c.get('content_truncated') is not False
        or not isinstance(nodes,list) or not nodes or not c.get('scope')): return False
    if c.get('revision_before')!=c.get('revision_after'): return False
    for n in nodes:
        if n.get('status') not in {'complete','excluded_self_sender'}: return False
        for expected,actual in [('pages_expected','pages_read'),('frames_expected','frames_read'),('entries_expected','entries_read'),('blocks_expected','blocks_read'),('cells_expected','cells_read')]:
            if expected in n and n[expected]!=n.get(actual): return False
    chunks=[{'index':i//24000,'chars':len(text[i:i+24000]),'sha256':digest(text[i:i+24000].encode())} for i in range(0,len(text),24000)]
    return c.get('text_chunk_size')==24000 and c.get('text_chunks')==chunks

def cache_key(profile):
    required={'representation','raw_sha256','raw_bytes','view_sha256','view_bytes','parser_version','parser_sha256','scope','redaction','exclusions','domain','purpose','permission_epoch','ocr','format'}
    if not required<=profile.keys() or any(profile[x] in (None,'') for x in required-{'ocr'}): raise ValueError('cache_profile_incomplete')
    return key(profile)

class Registry:
    def __init__(self,path,readonly=False):
        self.path=Path(path);self.readonly=readonly
        if readonly:return
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.path.is_symlink(): raise AccessDenied('registry_symlink')
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS events(
              id TEXT PRIMARY KEY,source_key TEXT NOT NULL,event_id TEXT NOT NULL,source_json TEXT NOT NULL,
              domain TEXT NOT NULL,purpose TEXT NOT NULL,epoch TEXT NOT NULL,observed REAL NOT NULL,
              lane TEXT NOT NULL,ordinal INTEGER NOT NULL,metadata TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_source ON events(source_key,ordinal);
            CREATE TABLE IF NOT EXISTS access(source_key TEXT PRIMARY KEY,domain TEXT NOT NULL,purpose TEXT NOT NULL,epoch TEXT NOT NULL,state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS relations(id TEXT PRIMARY KEY,left_id TEXT NOT NULL,right_id TEXT NOT NULL,kind TEXT NOT NULL,scope TEXT NOT NULL,basis TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS corrections(id TEXT PRIMARY KEY,relation_id TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts(cache_key TEXT PRIMARY KEY,event_id TEXT NOT NULL,profile TEXT NOT NULL,product TEXT NOT NULL,output_sha TEXT NOT NULL,snapshot TEXT NOT NULL,generation INTEGER NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS leases(cache_key TEXT PRIMARY KEY,generation INTEGER NOT NULL,owner TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,event_id TEXT NOT NULL,action TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,event_id TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending');
            ''')
        if os.name!='nt': os.chmod(self.path,0o600)
    def connect(self):
        db=sqlite3.connect('file:'+str(self.path)+'?mode=ro',uri=True,timeout=3) if self.readonly else sqlite3.connect(self.path,timeout=3);db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON');return db
    @contextmanager
    def transaction(self):
        db=self.connect()
        try:
            db.execute('BEGIN IMMEDIATE');yield db;db.commit()
        except BaseException: db.rollback();raise
        finally: db.close()
    @staticmethod
    def context(ctx):
        if ctx.get('authorized') is not True or not all(ctx.get(k) for k in ('domain','purpose','permission_epoch')): raise AccessDenied('source_authorization_required')
    def allowed(self,db,event,ctx):
        self.context(ctx)
        if event is None: raise AccessDenied('source_unavailable')
        row=db.execute('SELECT * FROM access WHERE source_key=?',(event['source_key'],)).fetchone()
        if not row or row['state']!='allowed' or any(row[k]!=ctx[c] for k,c in [('domain','domain'),('purpose','purpose'),('epoch','permission_epoch')]) or any(event[k]!=ctx[c] for k,c in [('domain','domain'),('purpose','purpose'),('epoch','permission_epoch')]): raise AccessDenied('source_access_changed')
    def register(self,source,event_id,ctx,metadata,*,observed=None,lane='live',ordinal=None):
        self.context(ctx)
        if not source or not event_id or lane not in {'live','backfill'}: raise ValueError('source_identity_required')
        sid=key([ctx['domain'],source]); eid=key([sid,str(event_id)])
        with self.transaction() as db:
            prior=db.execute('SELECT * FROM events WHERE id=?',(eid,)).fetchone()
            if prior:
                self.allowed(db,prior,ctx);return eid
            acl=db.execute('SELECT * FROM access WHERE source_key=?',(sid,)).fetchone()
            if acl and (acl['state']!='allowed' or acl['epoch']!=ctx['permission_epoch'] or acl['purpose']!=ctx['purpose']): raise AccessDenied('explicit_reauthorization_required')
            db.execute('INSERT OR IGNORE INTO access VALUES(?,?,?,?,?)',(sid,ctx['domain'],ctx['purpose'],ctx['permission_epoch'],'allowed'))
            if ordinal is None:
                ordinal=db.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM events WHERE source_key=? AND lane=?',(sid,lane)).fetchone()[0]
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)',(eid,sid,str(event_id),canonical(source),ctx['domain'],ctx['purpose'],ctx['permission_epoch'],time.time() if observed is None else observed,lane,ordinal,canonical(metadata)))
            self._receipt(db,eid,'source_registered',{'lane':lane})
        return eid
    def _receipt(self,db,event,action,payload):
        db.execute('INSERT INTO receipts VALUES(?,?,?,?,?)',(uuid.uuid4().hex,event,action,canonical(payload),time.time()))
    def receipt(self,event,ctx,action,payload):
        with self.transaction() as db:
            self.allowed(db,db.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone(),ctx);self._receipt(db,event,action,payload)
    def set_access(self,source,ctx,state,new_epoch=None):
        self.context(ctx)
        if state not in {'allowed','unavailable','revoked','deleted'}: raise ValueError('access_state_invalid')
        sid=key([ctx['domain'],source])
        with self.transaction() as db:
            db.execute('UPDATE access SET state=?,epoch=? WHERE source_key=? AND domain=?',(state,new_epoch or ctx['permission_epoch'],sid,ctx['domain']))
    def relate(self,left,right,kind,scope,basis,ctx):
        if kind not in RELATIONS: raise ValueError('relation_invalid')
        # Exact relations can only be created by compare(), with transient verified bytes.
        if kind=='bytes_equal' and basis.get('_verified_bytes') is not True: raise ValueError('byte_comparison_required')
        basis={k:v for k,v in basis.items() if k!='_verified_bytes'};rid=key([left,right,kind,scope,basis])
        with self.transaction() as db:
            for e in (left,right):self.allowed(db,db.execute('SELECT * FROM events WHERE id=?',(e,)).fetchone(),ctx)
            db.execute('INSERT OR IGNORE INTO relations VALUES(?,?,?,?,?,?,?)',(rid,left,right,kind,scope,canonical(basis),time.time()))
        return rid
    def compare(self,left,right,ctx,left_snapshot,right_snapshot):
        if left_snapshot.get('sha256')!=right_snapshot.get('sha256') or left_snapshot.get('bytes')!=right_snapshot.get('bytes'):return None
        with self.connect() as db:
            for e in (left,right):self.allowed(db,db.execute('SELECT * FROM events WHERE id=?',(e,)).fetchone(),ctx)
        verified=False;reason='snapshot_changed_or_bytes_differ'
        try:
            la,a=snapshot(left_snapshot['path']);rb,b=snapshot(right_snapshot['path'])
            verified=(la['sha256']==left_snapshot['sha256'] and rb['sha256']==right_snapshot['sha256'] and la['bytes']==left_snapshot['bytes'] and rb['bytes']==right_snapshot['bytes'] and a==b)
        except (OSError,KeyError):reason='snapshot_unavailable'
        except StaleWork:reason='snapshot_changed_during_read'
        return self.relate(left,right,'bytes_equal' if verified else 'candidate','raw_bytes',{'method':'stable_snapshot_byte_compare' if verified else 'fingerprint_only','sha256':left_snapshot['sha256'],'bytes':left_snapshot['bytes'],'_verified_bytes':verified,'independent_evidence_increment':0,'reason':'verified' if verified else reason},ctx)
    def correct(self,relation,reason,ctx):
        with self.transaction() as db:
            r=db.execute('SELECT * FROM relations WHERE id=?',(relation,)).fetchone()
            if not r:raise ValueError('relation_missing')
            for e in (r['left_id'],r['right_id']):self.allowed(db,db.execute('SELECT * FROM events WHERE id=?',(e,)).fetchone(),ctx)
            cid=key([relation,reason]);db.execute('INSERT OR IGNORE INTO corrections VALUES(?,?,?,?)',(cid,relation,reason,time.time()))
            db.execute('INSERT OR IGNORE INTO outbox(id,event_id,kind,payload) VALUES(?,?,?,?)',(cid,r['left_id'],'refresh_derived_relation',canonical({'relation':relation,'reason':reason,'business_writer':'original_verification_entry'})))
    def claim(self,event,ctx,profile,seconds=1800):
        ck=cache_key(profile)
        if any(profile[k]!=ctx[k] for k in ('domain','purpose','permission_epoch')):raise AccessDenied('cache_domain_mismatch')
        with self.transaction() as db:
            self.allowed(db,db.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone(),ctx)
            old=db.execute('SELECT * FROM leases WHERE cache_key=?',(ck,)).fetchone();t=time.time()
            if old and old['expires']>t:return None
            generation=old['generation']+1 if old else 1;owner=uuid.uuid4().hex
            db.execute('INSERT OR REPLACE INTO leases VALUES(?,?,?,?)',(ck,generation,owner,t+seconds))
            return {'key':ck,'generation':generation,'owner':owner}
    def commit(self,event,ctx,profile,lease,product,source_snapshot):
        if not valid_product(product):return False
        if product['coverage']['scope']!=profile['scope']:raise ValueError('coverage_scope_mismatch')
        if cache_key(profile)!=lease['key']:raise ValueError('cache_profile_changed')
        current,_=snapshot(source_snapshot['path'])
        if current['sha256']!=profile['view_sha256'] or current['bytes']!=profile['view_bytes']:raise StaleWork('source_changed_before_commit')
        if product['coverage']['nodes'][0].get('sha256')!=profile['view_sha256'] or product['coverage']['nodes'][0].get('bytes')!=profile['view_bytes']:raise ValueError('product_source_unbound')
        if any(profile[k]!=ctx[k] for k in ('domain','purpose','permission_epoch')):raise AccessDenied('cache_domain_mismatch')
        raw=canonical(product)
        with self.transaction() as db:
            self.allowed(db,db.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone(),ctx)
            l=db.execute('SELECT * FROM leases WHERE cache_key=?',(lease['key'],)).fetchone()
            if not l or l['generation']!=lease['generation'] or l['owner']!=lease['owner'] or l['expires']<time.time():raise StaleWork('lease_superseded')
            db.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?,?,?,?,?)',(lease['key'],event,canonical(profile),raw,digest(raw.encode()),canonical(source_snapshot),lease['generation'],time.time()))
            self._receipt(db,event,'cache_committed',{'cache_key':lease['key'],'generation':lease['generation']})
            oid=key([event,lease['key'],lease['generation']])
            db.execute('INSERT OR IGNORE INTO outbox(id,event_id,kind,payload) VALUES(?,?,?,?)',(oid,event,'source_result',canonical({'cache_key':lease['key']})))
            db.execute('UPDATE leases SET expires=0 WHERE cache_key=? AND generation=?',(lease['key'],lease['generation']))
        return True
    def lookup(self,event,ctx,profile,current_data):
        ck=cache_key(profile)
        if any(profile[k]!=ctx[k] for k in ('domain','purpose','permission_epoch')):raise AccessDenied('cache_domain_mismatch')
        if digest(current_data)!=profile['view_sha256'] or len(current_data)!=profile['view_bytes']:raise StaleWork('current_view_changed')
        with self.connect() as db:
            self.allowed(db,db.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone(),ctx)
            a=db.execute('SELECT * FROM artifacts WHERE cache_key=?',(ck,)).fetchone()
            if not a:return None
            self.allowed(db,db.execute('SELECT * FROM events WHERE id=?',(a['event_id'],)).fetchone(),ctx)
        try:
            if digest(a['product'].encode())!=a['output_sha']:return None
            p=json.loads(a['product']);s=json.loads(a['snapshot']);current,prior=snapshot(s['path'])
            if not valid_product(p) or current['sha256']!=profile['view_sha256'] or prior!=current_data:return None
        except (OSError,ValueError,KeyError,StaleWork):return None
        # Recheck both grants after potentially slow verification.
        with self.transaction() as db:
            for e in (event,a['event_id']):self.allowed(db,db.execute('SELECT * FROM events WHERE id=?',(e,)).fetchone(),ctx)
            self._receipt(db,event,'cache_hit',{'cache_key':ck,'generation':a['generation'],'byte_comparison':True})
        return {'product':p,'cache_key':ck,'generation':a['generation'],'source_event':a['event_id']}
    def acknowledge(self,event,ctx):
        with self.transaction() as db:
            self.allowed(db,db.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone(),ctx)
            db.execute("UPDATE outbox SET status='done' WHERE event_id=? AND kind='source_result'",(event,))
    def listing(self,ctx,limit=50,offset=0,identifier=None):
        self.context(ctx);items=[]
        with self.connect() as db:
            rows=db.execute('SELECT e.* FROM events e JOIN access a USING(source_key) WHERE a.state=? AND a.domain=? AND a.purpose=? AND a.epoch=? AND e.epoch=a.epoch AND e.purpose=a.purpose AND e.domain=a.domain ORDER BY e.observed DESC,e.ordinal DESC',('allowed',ctx['domain'],ctx['purpose'],ctx['permission_epoch'])).fetchall()
            visible={r['id'] for r in rows}
            rels=[dict(r) for r in db.execute('SELECT * FROM relations WHERE id NOT IN (SELECT relation_id FROM corrections)') if r['left_id'] in visible and r['right_id'] in visible]
            selected=[r for r in rows if not identifier or r['id']==identifier]
            for r in selected[max(0,offset):max(0,offset)+min(max(1,limit),200)]:
                item=dict(r);item['source']=json.loads(item.pop('source_json'));item['metadata']=json.loads(item['metadata']);item['relations']=[x for x in rels if r['id'] in (x['left_id'],x['right_id'])]
                item['receipts']=[{'action':x['action'],'payload':json.loads(x['payload']),'created':x['created']} for x in db.execute('SELECT * FROM receipts WHERE event_id=? ORDER BY created',(r['id'],))]
                if not identifier:
                    item['receipts']=[{'action':x['action'],'created':x['created'],'payload':{k:v for k,v in x['payload'].items() if k in {'extract_status','cache','source_sha256','source_bytes','view_sha256','business_event_skipped','current_job_id'}}} for x in item['receipts'][-3:]]
                items.append(item)
            metrics={x['action']:x['n'] for x in db.execute('SELECT r.action,COUNT(*) n FROM receipts r JOIN events e ON e.id=r.event_id JOIN access a USING(source_key) WHERE a.state=? AND a.domain=? AND a.purpose=? AND a.epoch=? AND e.epoch=a.epoch AND e.purpose=a.purpose AND e.domain=a.domain GROUP BY r.action',('allowed',ctx['domain'],ctx['purpose'],ctx['permission_epoch']))}
        return {'items':items,'total':len(selected),'relations':len(rels),'metrics':metrics,'independence_rule':'copies_do_not_add_independent_evidence'}
