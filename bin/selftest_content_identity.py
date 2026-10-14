#!/usr/bin/env python3
"""32 approved scenarios, exercised with isolated source/event copies."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import asyncio, copy, json, sqlite3, tempfile, time, unittest, zipfile
from pathlib import Path
from unittest.mock import patch
from content_identity import *
from mail_attachment_reader import Reader

class Cases(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.r=Registry(self.root/'registry.sqlite')
        self.ctx={'authorized':True,'domain':'test-owner','purpose':'business-memory','permission_epoch':'1'}
        self.a=self.event('computer','A',b'Hello');self.b=self.event('cloud','B',b'Hello')
    def tearDown(self):self.tmp.cleanup()
    def event(self,source,event,data,**kwargs):
        p=self.root/(source+'-'+event+'.txt');p.write_bytes(data);s,_=snapshot(p)
        e=self.r.register({'system':source,'id':source},event,self.ctx,{'name':p.name,'snapshot':s},**kwargs)
        return e,s
    def profile(self,s,**kwargs):
        return {'representation':'raw','raw_sha256':s['sha256'],'raw_bytes':s['bytes'],'view_sha256':s['sha256'],'view_bytes':s['bytes'],'parser_version':'v1','parser_sha256':'abc','scope':'machine-text','redaction':'none','exclusions':'self-v1',**{k:self.ctx[k] for k in ['domain','purpose','permission_epoch']},'ocr':{'model':'none','language':'none','config':'none'},'format':'.txt',**kwargs}
    def product(self,s,text='Hello'):
        return {'text':text,'text_sha256':digest(text.encode()),'text_bytes':len(text.encode()),'coverage':{'complete':True,'content_truncated':False,'scope':'machine-text','nodes':[{'status':'complete','sha256':s['sha256'],'bytes':s['bytes']}],'text_chunk_size':24000,'text_chunks':[{'index':0,'chars':len(text),'sha256':digest(text.encode())}] if text else []}}
    def cache(self,a=None,p=None):
        e,s=a or self.a;p=p or self.profile(s);l=self.r.claim(e,self.ctx,p);self.assertTrue(self.r.commit(e,self.ctx,p,l,self.product(s),s));return p
    def hit(self,b=None,p=None):
        e,s=b or self.b;return self.r.lookup(e,self.ctx,p or self.profile(s),Path(s['path']).read_bytes())
    def test_01_same_name_content_origins(self):
        p=self.cache();self.assertIsNotNone(self.hit(p=p));self.assertEqual(self.r.listing(self.ctx)['total'],2);self.assertEqual(Path(self.a[1]['path']).read_bytes(),b'Hello')
    def test_02_different_names_equal_bytes(self):
        rid=self.r.compare(self.a[0],self.b[0],self.ctx,self.a[1],self.b[1]);self.assertIsNotNone(rid);self.assertEqual(self.r.listing(self.ctx)['items'][0]['relations'][0]['kind'],'bytes_equal')
    def test_03_same_name_different_content(self):
        c=self.event('cloud','C',b'Changed');self.cache();self.assertIsNone(self.hit(c));self.assertIsNone(self.r.compare(self.a[0],c[0],self.ctx,self.a[1],c[1]))
    def test_04_shared_template_separate_projects(self):
        self.cache();self.hit();items=self.r.listing(self.ctx)['items'];self.assertNotEqual(items[0]['source_key'],items[1]['source_key'])
    def test_05_replaced_path_no_stale_hit(self):
        self.cache();Path(self.a[1]['path']).write_bytes(b'Replaced');self.assertIsNone(self.hit())
    def test_06_stable_id_move_history(self):
        a=self.event('computer','renamed',b'Hello');self.assertNotEqual(a[0],self.a[0]);self.assertEqual(len({x['source_key'] for x in self.r.listing(self.ctx)['items'] if x['source']['system']=='computer'}),1)
    def test_07_unreliable_id_no_move_inference(self):
        self.r.relate(self.a[0],self.b[0],'candidate','location',{'reason':'unstable_id'},self.ctx);self.assertEqual(self.r.listing(self.ctx)['items'][0]['relations'][0]['kind'],'candidate')
    def test_08_cross_format_media_difference(self):
        self.cache();self.assertIsNone(self.hit(p=self.profile(self.b[1],representation='adoc-jsonml')))
    def test_09_excel_formula_difference(self):
        self.cache();c=self.event('sheet','formula',b'<f>A1*2</f><v>10</v>');self.assertIsNone(self.hit(c))
    def test_10_hidden_format_comments(self):
        for b in [b'<sheet state="hidden"/>',b'<numFmt formatCode="0%"/>',b'<comment>New</comment>']:
            c=self.event('sheet',digest(b)[:8],b);self.assertNotEqual(self.profile(c[1])['raw_sha256'],self.profile(self.a[1])['raw_sha256'])
    def test_11_false_A1_dimension_real_cells(self):
        p=self.root/'small.xlsx'
        with zipfile.ZipFile(p,'w') as z:z.writestr('xl/worksheets/sheet1.xml','<worksheet><dimension ref="A1"/><sheetData><row><c r="A1"><v>1</v></c><c r="G42"><v>42</v></c></row></sheetData></worksheet>')
        reader=Reader(self.root);asyncio.run(reader.read(p.read_bytes(),'original.xlsx'));self.assertIn('G42=42','\n'.join(reader.parts));self.assertEqual(reader.nodes[0]['status'],'complete')
    def test_12_ocr_text_not_source_identity(self):
        self.cache();self.assertIsNone(self.hit(p=self.profile(self.b[1],ocr={'model':'different','language':'zh','config':'v2'})))
    def test_13_identical_encrypted_failed_not_full(self):
        self.r.compare(self.a[0],self.b[0],self.ctx,self.a[1],self.b[1]);p=self.profile(self.a[1]);prod=self.product(self.a[1]);prod['coverage']['nodes'][0]['status']='failed';l=self.r.claim(self.a[0],self.ctx,p);self.assertFalse(self.r.commit(self.a[0],self.ctx,p,l,prod,self.a[1]));self.assertIsNone(self.hit())
    def test_14_new_parser_can_retry(self):
        p=self.profile(self.a[1]);self.r.claim(self.a[0],self.ctx,p);new=self.profile(self.a[1],parser_version='v2');self.assertIsNotNone(self.r.claim(self.a[0],self.ctx,new))
    def test_15_distinct_mail_events(self):
        a=self.event('mail','send1',b'Hello');b=self.event('mail','send2',b'Hello');self.cache(a);self.assertIsNotNone(self.hit(b));self.assertNotEqual(a[0],b[0])
    def test_16_copy_not_independent_support(self):
        rid=self.r.compare(self.a[0],self.b[0],self.ctx,self.a[1],self.b[1]);v=self.r.listing(self.ctx);self.assertEqual(json.loads(v['items'][0]['relations'][0]['basis'])['independent_evidence_increment'],0)
    def test_17_independent_branches(self):
        self.event('computer','v2',b'X');self.event('cloud','v2',b'Y');self.assertEqual(self.r.listing(self.ctx)['total'],4)
    def test_18_similarity_not_transitive(self):
        c=self.event('third','C',b'Hi');self.r.relate(self.a[0],self.b[0],'candidate','text',{},self.ctx);self.r.relate(self.b[0],c[0],'candidate','text',{},self.ctx);self.assertEqual(self.r.listing(self.ctx)['relations'],2)
    def test_19_offline_404_403_no_borrow(self):
        self.cache();self.r.set_access({'system':'cloud','id':'cloud'},self.ctx,'unavailable')
        with self.assertRaises(AccessDenied):self.hit()
        with self.r.connect() as d:self.assertEqual(d.execute('select count(*) from events').fetchone()[0],2)
    def test_20_delete_one_retains_other(self):
        self.r.set_access({'system':'cloud','id':'cloud'},self.ctx,'deleted');self.assertEqual(self.r.listing(self.ctx)['total'],1)
    def test_21_idempotence_fencing(self):
        e=self.r.register({'system':'computer','id':'computer'},'A',self.ctx,{});self.assertEqual(e,self.a[0]);p=self.profile(self.a[1]);old=self.r.claim(e,self.ctx,p,seconds=-1);new=self.r.claim(self.b[0],self.ctx,p);self.assertIsNone(self.r.claim(e,self.ctx,p))
        with self.assertRaises(StaleWork):self.r.commit(e,self.ctx,p,old,self.product(self.a[1]),self.a[1])
        self.assertTrue(self.r.commit(self.b[0],self.ctx,p,new,self.product(self.b[1]),self.b[1]))
    def test_22_backfill_separate_order(self):
        self.event('computer','live2',b'Live',ordinal=20);self.event('computer','old',b'Old',ordinal=1,lane='backfill',observed=1)
        with self.r.connect() as d:self.assertEqual(d.execute("select max(ordinal) from events where lane='live'").fetchone()[0],20)
    def test_23_crash_and_outbox(self):
        p=self.profile(self.a[1]);self.r.claim(self.a[0],self.ctx,p,seconds=-1);self.assertIsNone(self.hit());self.cache();self.r.acknowledge(self.a[0],self.ctx);self.r.acknowledge(self.a[0],self.ctx)
        with self.r.connect() as d:self.assertEqual(d.execute("select count(*) from outbox where status='pending'").fetchone()[0],0)
    def test_24_corrupt_cache(self):
        self.cache()
        with self.r.connect() as d:d.execute("update artifacts set product='corrupt'")
        self.assertIsNone(self.hit())
    def test_25_exact_self_derived_exclusion(self):
        from pathlib import PurePosixPath
        excluded={'同步看板.html'};self.assertIn('同步看板.html',excluded);self.assertNotIn('客户/同步看板.html',excluded)
        s=Path(__file__).with_name('content_observer.py').read_text();self.assertIn("x['source_relative_path'] in exclusions",s)
    def test_26_success_flag_contradiction(self):
        p=self.product(self.a[1]);p['coverage']['nodes'][0].update(blocks_expected=10,blocks_read=9);self.assertFalse(valid_product(p))
    def test_27_old_relation_scoped_versions(self):
        self.r.compare(self.a[0],self.b[0],self.ctx,self.a[1],self.b[1]);e=self.event('computer','new',b'New');self.assertFalse(self.r.listing(self.ctx,identifier=e[0])['items'][0]['relations'])
    def test_28_off_preserves_new_events(self):
        self.cache();self.event('computer','afteroff',b'After');self.assertEqual(self.r.listing(self.ctx)['total'],3)
        import content_registry
        cfg={'cross_source_policy':str(self.root/'absent')};self.assertEqual(content_registry.policy(cfg)['mode'],'off')
    def test_29_mixed_revision_not_full(self):
        p=self.product(self.a[1]);p['coverage'].update(revision_before='A',revision_after='B');self.assertFalse(valid_product(p))
    def test_30_A_B_A_three_versions(self):
        self.cache();self.event('computer','2',b'B');a=self.event('computer','3',b'Hello');self.assertIsNotNone(self.hit(a));self.assertEqual(self.r.listing(self.ctx,identifier=a[0])['items'][0]['ordinal'],3)
    def test_31_revoke_during_parse_hides_counts(self):
        p=self.profile(self.a[1]);l=self.r.claim(self.a[0],self.ctx,p);self.r.set_access({'system':'computer','id':'computer'},self.ctx,'revoked')
        with self.assertRaises(AccessDenied):self.r.commit(self.a[0],self.ctx,p,l,self.product(self.a[1]),self.a[1])
        v=self.r.listing(self.ctx);self.assertEqual(v['total'],1);self.assertEqual(v['relations'],0)
    def test_32_correct_append_refresh_original_writer(self):
        rel=self.r.compare(self.a[0],self.b[0],self.ctx,self.a[1],self.b[1]);self.r.correct(rel,'incorrect_association',self.ctx);self.assertEqual(self.r.listing(self.ctx)['relations'],0)
        with self.r.connect() as d:
            self.assertEqual(d.execute('select count(*) from relations').fetchone()[0],1);self.assertEqual(d.execute("select kind from outbox").fetchone()[0],'refresh_derived_relation')

if __name__=='__main__':unittest.main(verbosity=2)
