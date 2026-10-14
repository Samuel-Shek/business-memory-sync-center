"""Bounded content classification metadata; never changes provider selection or facts."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib,importlib,importlib.util,os,sys,threading,time
from pathlib import Path

ADAPTER_VERSION=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

def load_client():
    name='_example_jev_client'
    if name not in sys.modules:
        root=Path.home()/'Code/active/jev-judgment-service/jev'
        spec=importlib.util.spec_from_file_location(name,root/'__init__.py',submodule_search_locations=[str(root)])
        module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return importlib.import_module(name+'.client')

class Classification:
    def __init__(self,text,purpose,*,disabled=False,client=None):
        self.lock=threading.RLock();self.alive=True;self.value=None;self.thread=None
        self.end=time.time()+10
        if disabled or os.environ.get('JEV_DISABLE')=='1' or (client is None and ('unittest' in sys.modules or 'pytest' in sys.modules)):return
        if not isinstance(text,str) or not text.strip() or len(text.encode())>20000:return
        self.client=client or load_client();self.state={'text':text}
        self.source=hashlib.sha256(text.encode()).hexdigest()
        self.object=hashlib.sha256((purpose+':'+self.source).encode()).hexdigest()
        self.thread=threading.Thread(target=self.work,name='jev-content-classification',daemon=True);self.thread.start()
    def valid(self):
        with self.lock:return self.alive and time.time()<self.end
    def work(self):
        try:
            release=self.client.scenario('bridge','content_class')
            if release.get('mode') not in ('shadow','assist') or not self.valid():return
            if release['mode']=='assist' and release.get('adapter_version')!=ADAPTER_VERSION:return
            result=self.client.request('bridge','content_class',self.state,source_version=self.source,
                    object_version=self.object,adapter_version=ADAPTER_VERSION,event_id=self.object,
                    data_class='business_text',deadline=self.end)
            if not self.valid() or result.get('mode')!='assist':return
            def apply(label):
                with self.lock:
                    if not self.valid() or label not in ('business','technical','other'):raise ValueError('classification_invalidated')
                    self.value={'label':label,'request_id':result['request_id'],'release_version':result['release_version'],
                                'source_version':self.source,'adapter_version':ADAPTER_VERSION}
                    return label
            self.client.consume('bridge',result,state=self.state,source_version=self.source,object_version=self.object,
                                adapter_version=ADAPTER_VERSION,still_valid=self.valid,apply=apply)
        except Exception:pass
    def finish(self):
        with self.lock:
            self.alive=False
            return dict(self.value) if self.value else None
