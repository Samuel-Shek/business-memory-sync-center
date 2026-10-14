"""Windows-only full attachment extraction. Originals never leave Windows.

Every content leaf has a receipt. Resource/format failures propagate to the root;
there is no character-count success cutoff. OCR is machine text recognition,
not a claim that a diagram or the business facts have been understood.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import argparse, asyncio, base64, email.policy, hashlib, io, json, os, re, socket
import struct, subprocess, sys, time, zipfile, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.parser import BytesParser
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

VERSION='win11-full-content-v5-office-signature'
from video_hold import classify as media_classify, POLICY as VIDEO_POLICY
import sqlite3, importlib.metadata
from content_identity import Registry, AccessDenied, StaleWork, cache_key, key, valid_product
MAX_EXPANDED=512*1024*1024
ACTIVE={'.exe','.dll','.msi','.com','.scr','.bat','.cmd','.ps1','.vbs','.js','.hta','.lnk','.jar'}
IMAGE={'.png','.jpg','.jpeg','.avif','.bmp','.dib','.tif','.tiff','.gif','.webp','.ico','.emf','.wmf'}
TEXT={'.txt','.md','.csv','.tsv','.json','.xml','.yaml','.yml','.log','.ini','.css'}
SELF={'fixture_9413a2bc@example.invalid','fixture_f2e4de31@example.invalid'}
def sha(data):return hashlib.sha256(data).hexdigest()
def now():return datetime.now(timezone.utc).isoformat()
def atomic(path, value):
    p=Path(path);t=p.with_suffix(p.suffix+'.tmp');t.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');t.replace(p)
def local_name(tag):return tag.rsplit('}',1)[-1]
def xml(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():raise ValueError('xml_external_entity_rejected')
    return ET.fromstring(data)
def decode(data, charset=None):
    candidates=([charset] if charset else [])+['utf-8-sig','utf-16' if data[:2] in (b'\xff\xfe',b'\xfe\xff') else 'utf-8','gb18030','cp1252']
    for enc in dict.fromkeys(candidates):
        try:
            s=data.decode(enc)
            if '\x00' in s:continue
            return s
        except (UnicodeError,LookupError):pass
    raise ValueError('text_encoding_unresolved')
def ole_native_payload(data):
    """Read the length-delimited MS-OLEDS native payload without executing it."""
    cursor=0
    def number(size):
        nonlocal cursor
        value=int.from_bytes(data[cursor:cursor+size],'little')
        if cursor+size>len(data):raise ValueError('ole_header_incomplete')
        cursor+=size;return value
    def string():
        nonlocal cursor
        end=data.find(b'\0',cursor)
        if end<0:raise ValueError('ole_name_incomplete')
        value=data[cursor:end].decode('cp1252',errors='replace');cursor=end+1;return value
    declared=number(4);number(2);name=string();string();number(4);number(4);string()
    if cursor==len(data):return name,None
    count=number(4)
    if count>len(data)-cursor:raise ValueError('ole_payload_incomplete')
    return name,data[cursor:cursor+count]
def ole_native_prefix(stream,size):
    """Read CFB directories and at most 4096 native-header bytes, never a whole stream."""
    stream.seek(0);header=stream.read(512)
    if len(header)!=512 or header[:8]!=bytes.fromhex('d0cf11e0a1b11ae1') or header[28:30]!=b'\xfe\xff':raise ValueError('ole_header_invalid')
    sector_size=1<<int.from_bytes(header[30:32],'little')
    if sector_size not in (512,4096):raise ValueError('ole_sector_size')
    def sector(index):
        offset=(index+1)*sector_size
        if index>=0xfffffffa or offset+sector_size>size:raise ValueError('ole_sector_bounds')
        stream.seek(offset);value=stream.read(sector_size)
        if len(value)!=sector_size:raise ValueError('ole_sector_short')
        return value
    count=int.from_bytes(header[44:48],'little')
    if not 1<=count<=109:raise ValueError('ole_fat_budget')
    ids=struct.unpack('<109I',header[76:512]);fat=[]
    for index in ids[:count]:fat.extend(struct.unpack('<'+str(sector_size//4)+'I',sector(index)))
    current=int.from_bytes(header[48:52],'little');seen=set();found=[]
    for _ in range(32):
        if current==0xfffffffe:break
        if current in seen or current>=len(fat):raise ValueError('ole_directory_cycle')
        seen.add(current);data=sector(current)
        for offset in range(0,len(data),128):
            entry=data[offset:offset+128];length=int.from_bytes(entry[64:66],'little')
            if entry[66]!=2 or not 2<=length<=64 or length%2:continue
            if entry[:length-2].decode('utf-16le')=='\x01Ole10Native':
                found.append((int.from_bytes(entry[116:120],'little'),int.from_bytes(entry[120:128],'little')))
        current=fat[current]
    else:raise ValueError('ole_directory_budget')
    if len(found)!=1:raise ValueError('ole_native_identity')
    current,length=found[0]
    if not 4096<=length<=size:raise ValueError('ole_native_small_or_invalid')
    result=b'';seen=set()
    while len(result)<min(4096,length):
        if current in seen or current>=len(fat):raise ValueError('ole_native_cycle')
        seen.add(current);result+=sector(current);current=fat[current]
        try:
            ole_header(result)
            return result[:4096]
        except ValueError:pass
    raise ValueError('ole_native_header_budget')

def ole_header(data):
    """Bounded native metadata; the remaining payload is never needed to classify."""
    cursor=0
    def number(size):
        nonlocal cursor
        if cursor+size>len(data):raise ValueError('ole_header_incomplete')
        value=int.from_bytes(data[cursor:cursor+size],'little');cursor+=size;return value
    def string():
        nonlocal cursor
        end=data.find(b'\0',cursor)
        if end<0:raise ValueError('ole_header_incomplete')
        value=data[cursor:end].decode('cp1252');cursor=end+1;return value
    number(4);number(2);name=string();string();number(4);number(4);string();count=number(4)
    return name,count,data[cursor:cursor+16]

class HTMLText(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[];self.suppressed=0
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style'}:self.suppressed+=1
        if tag in {'p','br','div','tr','li'}:self.parts.append('\n')
        if tag=='img':
            for k,v in attrs:
                if k in {'alt','title'} and v:self.parts.append(v+'\n')
    def handle_endtag(self,tag):
        if tag in {'script','style'} and self.suppressed:self.suppressed-=1
    def handle_data(self,s):
        if not self.suppressed:self.parts.append(s)

class Reader:
    def __init__(self,job):
        self.job=Path(job);self.derived=self.job/'full-read-derived';self.derived.mkdir(exist_ok=True)
        self.nodes=[];self.parts=[];self.cache={};self.expanded=0;self.ocr_engines=None
    def defer(self,name,size=None,kind='video'):
        node={'name':name,'bytes':size,'sha256':None,'status':'deferred_video' if kind=='video' else 'deferred_media',
              'method':'metadata_only_waiting_instruction','policy':VIDEO_POLICY,'content_read':False}
        self.nodes.append(node);return node
    async def member(self,archive,info,parent,depth):
        name=parent+' / '+info.filename
        kind=media_classify(info.filename)
        if kind and Path(info.filename).suffix.lower()=='.bin':
            # Existing OOXML container only: directory plus a bounded native header.
            # Do not read/extract held video payloads to identify their type.
            try:
                with archive.open(info) as stream:
                    header=ole_native_prefix(stream,info.file_size)
                    native,count,prefix=ole_header(header)
                leaf=Path(native.replace('\\','/')).name
                native_kind=media_classify(leaf)
                if native_kind=='video':
                    node=self.defer(name+' / '+leaf,count,'video')
                    node['type_evidence']='ole_native_header';return node
                if Path(leaf).suffix.lower()=='.pdf' and prefix.startswith(b'%PDF-'):
                    return await self.read(archive.read(info),name,depth,verified_ole=True)
            except (OSError,ValueError,KeyError,ImportError):pass
        if kind:return self.defer(name,info.file_size,kind)
        return await self.read(archive.read(info),name,depth)
    def progress(self,stage):atomic(self.job/'full-read-progress.json',{'stage':stage,'nodes':len(self.nodes),'at':now()})
    def add(self,node,text):
        if text:
            node['text_sha256']=sha(text.encode());node['text_chars']=len(text)
            self.parts.append('['+node['name']+']\n'+text)
        else:node.update(text_chars=0,text_sha256=sha(b''))
    def derived_file(self,data,ext):
        p=self.derived/(sha(data)+ext)
        if not p.exists():p.write_bytes(data)
        return p
    async def ocr(self,image,label):
        from PIL import Image
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
        from winrt.windows.storage.streams import DataWriter
        if self.ocr_engines is None:
            self.ocr_engines=[]
            available=[x.language_tag for x in OcrEngine.available_recognizer_languages]
            for lang in available:
                if lang.startswith(('en','zh')):
                    engine=OcrEngine.try_create_from_language(Language(lang))
                    if engine:self.ocr_engines.append((lang,engine))
            if not self.ocr_engines:raise ValueError('windows_ocr_unavailable')
        # Tile rather than shrink/discard an over-size image; overlap preserves edge words.
        size=min(3500,OcrEngine.max_image_dimension);step=size-128;text=[];tiles=0
        for y in range(0,image.height,step):
            for x in range(0,image.width,step):
                crop=image.crop((x,y,min(x+size,image.width),min(y+size,image.height))).convert('RGBA')
                writer=DataWriter();bmp=None
                try:
                    writer.write_bytes(crop.tobytes('raw','BGRA'))
                    bmp=SoftwareBitmap.create_copy_with_alpha_from_buffer(writer.detach_buffer(),BitmapPixelFormat.BGRA8,crop.width,crop.height,BitmapAlphaMode.IGNORE)
                    for lang,engine in self.ocr_engines:
                        result=await engine.recognize_async(bmp)
                        if result.text.strip():text.append(f'[{lang} tile {x},{y}]\n'+result.text)
                finally:
                    if bmp is not None:bmp.close()
                    writer.close();crop.close()
                tiles+=1
        return '\n'.join(text),{'tiles':tiles,'languages':[v[0] for v in self.ocr_engines],'pixel_coverage':'all_tiles','empty_ocr_is_not_format_skip':True}
    async def image(self,data,node):
        from PIL import Image,ImageSequence
        with Image.open(io.BytesIO(data)) as im:
            count=getattr(im,'n_frames',1);frames=[];texts=[]
            for i in range(count):
                im.seek(i);frame=im.convert('RGB');text,coverage=await self.ocr(frame,node['name']);frames.append({'frame':i+1,'width':im.width,'height':im.height,**coverage});texts.append(text);frame.close()
        node.update(method='windows_image_decode_and_ocr',frames=frames,frames_expected=count,frames_read=len(frames),no_text_detected=not any(t.strip() for t in texts));self.add(node,'\n'.join(texts))
    async def pdf(self,data,node,depth):
        import pymupdf as fitz
        try:doc=fitz.open(stream=data,filetype='pdf')
        except Exception:return self.wps_pdf(self.derived_file(data,'.pdf'),node)
        try:
            if doc.needs_pass:raise ValueError('pdf_password_required')
            node.update(method='pdf_all_pages_text_and_ocr',pages_expected=len(doc),pages=[])
            for i,page in enumerate(doc):
                self.progress('pdf_page_'+str(i+1))
                text=page.get_text(sort=True)
                annotations=[a.info for a in page.annots() or []]
                widgets=[{'name':w.field_name,'label':w.field_label,'value':w.field_value} for w in page.widgets() or []]
                if annotations or widgets:text+='\n'+json.dumps({'annotations':annotations,'form_fields':widgets},ensure_ascii=False)
                # Full-page OCR catches text inside images even on mixed text/image pages.
                from PIL import Image
                ocr_parts=[];tile_count=0;rect=page.rect;y=rect.y0
                # Render bounded tiles, including large engineering drawings.
                while y<rect.y1:
                    x=rect.x0
                    while x<rect.x1:
                        clip=fitz.Rect(x,y,min(x+1500,rect.x1),min(y+1500,rect.y1))
                        pix=page.get_pixmap(dpi=144,alpha=False,clip=clip)
                        im=Image.frombytes('RGB',[pix.width,pix.height],pix.samples)
                        piece,cov=await self.ocr(im,node['name']);im.close();del pix
                        if piece:ocr_parts.append(piece)
                        tile_count+=cov['tiles'];x+=1468
                    y+=1468
                ocr='\n'.join(ocr_parts);cov={**cov,'tiles':tile_count}
                page_text=f'Page {i+1}\n{text}\n[OCR]\n{ocr}'
                node['pages'].append({'page':i+1,'native_chars':len(text),'ocr_chars':len(ocr),**cov})
                self.parts.append('['+node['name']+f' / page {i+1}]\n'+page_text)
            node['pages_read']=len(node['pages'])
            node['embedded_files']=doc.embfile_count()
            for name in doc.embfile_names():
                info=doc.embfile_info(name);kind=media_classify(info.get('filename') or name)
                if kind:self.defer(node['name']+' / embedded / '+name,info.get('size'),kind)
                else:await self.read(doc.embfile_get(name),node['name']+' / embedded / '+name,depth+1)
        finally:doc.close()
    def wps_pdf(self,path,node):
        import win32com.client
        app=win32com.client.GetActiveObject('KPDF.Application');doc=None;opened=False
        for i in range(1,app.Documents.Count+1):
            d=app.Documents.Item(i)
            if Path(d.FullName).resolve()==path.resolve():doc=d;break
        if doc is None:doc=app.Documents.Open(str(path.resolve()));opened=True
        try:
            if Path(doc.FullName).resolve()!=path.resolve():raise ValueError('reader_identity_mismatch')
            count=int(doc.PageCount);full=str(doc.DocumentText);pages=[str(doc.PageText(i)) for i in range(count)]
            if count<1 or len(full.encode('utf-16le'))//2!=int(doc.DocumentTextCount):raise ValueError('pdf_text_count_mismatch')
            if re.sub(r'\s','',full)!=re.sub(r'\s','',''.join(pages)):raise ValueError('pdf_page_text_mismatch')
            if any(not t.strip() for t in pages):raise ValueError('pdf_image_only_page_requires_ocr')
            node.update(method='wps_pdf_full_document_and_page_verification',pages_expected=count,pages_read=count,per_page_chars=[len(t) for t in pages],visual_ocr_available=False)
            self.add(node,full)
        finally:
            if opened and doc is not None:doc.Close()
    async def office(self,data,node,ext,depth):
        # CFB workbooks may contain an appended ZIP drawing package. The ZIP
        # scanner finding that package does not make the workbook OOXML.
        if data.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
            import olefile
            with olefile.OleFileIO(io.BytesIO(data)) as obj:
                streams=['/'.join(s) for s in obj.listdir()]
                if any(re.search(r'(^|/)(_VBA_PROJECT|VBA)(/|$)',s,re.I) for s in streams):
                    raise ValueError('active_office_content_requires_review')
                actual='.xls' if any(obj.exists(s) for s in ('Workbook','Book')) else '.doc' if obj.exists('WordDocument') else '.ppt' if obj.exists('PowerPoint Document') else None
                if actual is None:raise ValueError('office_container_not_recognized')
            node.update(detected_format=actual,container_signature='cfb')
            return await self.office_com(self.derived_file(data,actual),node,actual,depth)
        if not zipfile.is_zipfile(io.BytesIO(data)):
            if ext!='.rtf' or not data.lstrip().startswith(b'{\\rtf'):
                raise ValueError('office_container_not_recognized')
            return await self.office_com(self.derived_file(data,ext),node,ext,depth)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.check_zip(z)
            names=set(z.namelist())
            mains=[(p,k) for p,k in [('xl/workbook.xml','.xlsx'),('word/document.xml','.docx'),('ppt/presentation.xml','.pptx')] if p in names]
            if len(mains)!=1 or '[Content_Types].xml' not in names:
                raise ValueError('office_container_not_recognized')
            ext=mains[0][1]
            node.update(method='ooxml_all_content_parts_media_and_embeddings',detected_format=ext,container_signature='zip',content_parts=[],media_parts=[],embedded_parts=[])
            if ext=='.xlsx':
                import posixpath
                workbook=xml(z.read('xl/workbook.xml'))
                declared=[s for s in workbook.iter() if local_name(s.tag)=='sheet']
                if not declared or 'xl/_rels/workbook.xml.rels' not in names:raise ValueError('office_workbook_structure_invalid')
                relationships={r.get('Id'):r for r in xml(z.read('xl/_rels/workbook.xml.rels'))}
                expected=[]
                for sheet in declared:
                    rid=sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
                    rel=relationships.get(rid)
                    if rel is None or rel.get('TargetMode')=='External':raise ValueError('office_workbook_structure_invalid')
                    target=rel.get('Target','')
                    path=posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/'+target)
                    if path not in names or not path.startswith(('xl/worksheets/','xl/chartsheets/')):raise ValueError('office_workbook_structure_invalid')
                    expected.append(path)
                if len(set(expected))!=len(expected):raise ValueError('office_workbook_structure_invalid')
                node.update(sheets_expected=len(expected),sheets_read=0,sheet_parts=expected,cells_read=0,nonempty_cells=0)
            shared=[]
            if ext=='.xlsx' and 'xl/sharedStrings.xml' in z.namelist():
                shared=[''.join(el.text or '' for el in item.iter() if local_name(el.tag)=='t') for item in xml(z.read('xl/sharedStrings.xml')) if local_name(item.tag)=='si']
            for info in z.infolist():
                name=info.filename
                if info.is_dir():continue
                if re.search(r'(^|/)(vbaProject\.bin|activeX/)',name,re.I):raise ValueError('active_office_content_requires_review')
                if re.match(r'^(word|ppt|xl)/(media|embeddings)/',name):
                    (node['media_parts'] if '/media/' in name else node['embedded_parts']).append(name)
                    await self.member(z,info,node['name'],depth+1)
                elif name.endswith('.xml') and name.startswith(('word/','ppt/','xl/','docProps/')) and not any(x in name for x in ['/theme/','/slideLayouts/','/slideMasters/','/notesMasters/','styles.xml','fontTable.xml','settings.xml','webSettings.xml']):
                    tree=xml(z.read(info));texts=[]
                    if name.startswith('xl/worksheets/'):
                        for cell in tree.iter():
                            if local_name(cell.tag)!='c':continue
                            node['cells_read']+=1
                            value='';formula=''
                            for el in cell.iter():
                                if local_name(el.tag)=='v':value=el.text or ''
                                elif local_name(el.tag)=='t':value+=el.text or ''
                                elif local_name(el.tag)=='f':formula=el.text or ''
                            if cell.get('t')=='s':
                                if not value.isdigit() or int(value)>=len(shared):raise ValueError('office_shared_string_invalid')
                                value=shared[int(value)]
                            if value or formula:node['nonempty_cells']+=1
                            texts.append(cell.get('r','?')+'='+value+(' [formula='+formula+']' if formula else ''))
                        self.parts.append('['+node['name']+' / '+name+']\n'+'\n'.join(texts));node['content_parts'].append(name);continue
                    for el in tree.iter():
                        tag=local_name(el.tag)
                        if tag in {'t','v','f','title','subject','description','keywords','creator'} and el.text is not None:texts.append(el.text)
                        if tag=='sheet' and el.get('name'):texts.append('Sheet: '+el.get('name'))
                        if tag=='c' and el.get('r'):texts.append('Cell '+el.get('r'))
                    self.parts.append('['+node['name']+' / '+name+']\n'+'\n'.join(texts));node['content_parts'].append(name)
            node['entries_enumerated']=len(z.infolist())
            if not node['content_parts']:raise ValueError('office_content_not_recognized')
            if ext=='.xlsx':
                node['sheets_read']=sum(p in node['content_parts'] for p in node['sheet_parts'])
                if node['sheets_read']!=node['sheets_expected']:raise ValueError('office_workbook_structure_invalid')
                node['verified_empty_workbook']=node['nonempty_cells']==0 and not node['media_parts'] and not node['embedded_parts']
    async def office_com(self,path,node,ext,depth):
        import win32com.client
        prog='Excel.Application' if ext in {'.xlsx','.xls'} else 'Word.Application' if ext in {'.doc','.docx','.rtf'} else 'PowerPoint.Application'
        try:app=win32com.client.DispatchEx(prog)
        except Exception:raise ValueError('microsoft_office_reader_unavailable') from None
        security=app.AutomationSecurity;doc=None
        try:
            app.AutomationSecurity=3
            if app.AutomationSecurity!=3:raise ValueError('macro_disable_unavailable')
            output=self.derived/(node['sha256']+'-microsoft-readable'+('.xlsx' if prog=='Excel.Application' else '.docx' if prog=='Word.Application' else '.pptx'))
            if prog=='Excel.Application':
                app.Visible=False;app.DisplayAlerts=False
                doc=app.Workbooks.Open(str(path.resolve()),0,True)
                if Path(doc.FullName).resolve()!=path.resolve() or not doc.ReadOnly:raise ValueError('reader_identity_mismatch')
                if doc.HasVBProject:raise ValueError('active_office_content_requires_review')
                # Save to a separate derived file; the original remains read-only.
                if not output.exists():doc.SaveAs(str(output.resolve()),51)
            elif prog=='Word.Application':
                app.Visible=False;app.DisplayAlerts=0
                doc=app.Documents.Open(str(path.resolve()),False,True,False)
                if Path(doc.FullName).resolve()!=path.resolve():raise ValueError('reader_identity_mismatch')
                if not output.exists():doc.SaveAs(str(output.resolve()),16)
            else:
                doc=app.Presentations.Open(str(path.resolve()),True,False,False)
                if Path(doc.FullName).resolve()!=path.resolve():raise ValueError('reader_identity_mismatch')
                if not output.exists():doc.SaveAs(str(output.resolve()),24)
            node.update(method='windows_microsoft_office_readonly_derived_copy',office_application=prog,macro_security=3,update_links=0,original_modified=False)
        finally:
            if doc is not None:
                try:doc.Close(False)
                except Exception:
                    try:doc.Close()
                    except Exception:pass
            app.AutomationSecurity=security
            app.Quit()
        await self.read(output.read_bytes(),node['name']+' / readable-copy'+output.suffix,depth+1)
    def check_zip(self,z):
        infos=z.infolist();total=sum(x.file_size for x in infos)
        if len(infos)>20000 or total>MAX_EXPANDED or self.expanded+total>MAX_EXPANDED:raise ValueError('archive_expansion_requires_review')
        for item in infos:
            # Members are read by ZipInfo into bytes. Their names are labels only;
            # no member path is ever extracted to the filesystem.
            if item.flag_bits & 1:raise ValueError('archive_password_required')
        self.expanded+=total
    async def eml(self,data,node,depth):
        msg=BytesParser(policy=email.policy.default).parsebytes(data)
        if any(type(d).__name__ in {'StartBoundaryNotFoundDefect','CloseBoundaryNotFoundDefect','MultipartInvariantViolationDefect'} for d in msg.defects):raise ValueError('mail_mime_incomplete')
        node.update(method='mime_all_parts',parts_enumerated=0)
        self.add(node,'\n'.join(k+': '+str(v) for k,v in msg.items()))
        async def part(p,label,own=False):
            node['parts_enumerated']+=1
            if own and (p.get_content_disposition() in {'attachment','inline'} or p.get_filename() or not p.is_multipart() and p.get_content_maintype()!='text'):
                self.nodes.append({'name':label,'status':'excluded_self_sender','method':'exact_from_policy'});return
            if p.get_content_type()=='message/rfc822':
                for i,m in enumerate(p.get_payload()):await self.read(m.as_bytes(),label+f'/message-{i}.eml',depth+1)
            elif p.is_multipart():
                for i,ch in enumerate(p.iter_parts()):await part(ch,label+'/'+(ch.get_filename() or f'part-{i}'),own)
            else:
                ctype=p.get_content_type();name=p.get_filename()
                kind=media_classify(name or '',ctype)
                if kind:self.defer(label,None,kind);return
                content=p.get_payload(decode=True) or b''
                if p.defects:raise ValueError('mail_part_decode_defect')
                if ctype.startswith('text/') and not name:
                    t=decode(content,p.get_content_charset())
                    if ctype=='text/html':parser=HTMLText();parser.feed(t);t=''.join(parser.parts)
                    n=self.new_node(content,label);n.update(status='complete',method='mime_text_body',content_type=ctype);self.add(n,t)
                else:
                    ext=Path(name or '').suffix or { 'image/png':'.png','image/jpeg':'.jpg','image/avif':'.avif','application/pdf':'.pdf','text/plain':'.txt','text/html':'.html'}.get(ctype,'.dat')
                    await self.read(content,label+(ext if not Path(label).suffix else ''),depth+1)
        await part(msg,node['name'],parseaddr(str(msg.get('From') or ''))[1].casefold() in SELF)
    def new_node(self,data,name):
        n={'name':name,'sha256':sha(data),'bytes':len(data),'status':'reading'};self.nodes.append(n);return n
    async def ole(self,data,node,depth):
        import olefile
        with olefile.OleFileIO(io.BytesIO(data)) as obj:
            streams=obj.listdir();node.update(method='ole_embedded_payload_read',streams_enumerated=['/'.join(s) for s in streams]);found=0
            component=obj.openstream('\x01CompObj').read() if obj.exists('\x01CompObj') else b''
            for stream in streams:
                if stream[-1]=='\x01Ole10Native':
                    native=obj.openstream(stream).read()
                    if b'PBrush\0' in component and len(native)>=4 and int.from_bytes(native[:4],'little')==len(native)-4:
                        native_name,payload='embedded-paint-image.dib',native[4:]
                    else:native_name,payload=ole_native_payload(native)
                    if payload is None:
                        self.add(node,'Linked object metadata: '+native_name);node['external_link_not_fetched']=True
                    else:
                        await self.read(payload,node['name']+' / '+Path(native_name.replace('\\','/')).name,depth+1)
                    found+=1
                elif stream[-1]=='Package':
                    payload=obj.openstream(stream).read();ext='.zip'
                    if zipfile.is_zipfile(io.BytesIO(payload)):
                        with zipfile.ZipFile(io.BytesIO(payload)) as z:
                            names=z.namelist();ext='.xlsx' if 'xl/workbook.xml' in names else '.docx' if 'word/document.xml' in names else '.pptx' if 'ppt/presentation.xml' in names else '.zip'
                    await self.read(payload,node['name']+' / embedded'+ext,depth+1);found+=1
            if not found:raise ValueError('ole_payload_not_recognized')
            node['payloads_read']=found
    def raw_source_identity(self,path):
        if os.name!='nt':raise ValueError('source_hash_mismatch')
        # The installed enterprise file filter can expose the permitted clear
        # reading view to Python while PowerShell sees the original container.
        # Recheck the original container independently; do not ignore a mismatch.
        escaped=str(path.resolve()).replace("'","''")
        code="$p='"+escaped+"';$f=Get-Item -LiteralPath $p;@{sha256=(Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant();bytes=$f.Length}|ConvertTo-Json -Compress"
        cmd=['powershell.exe','-NoProfile','-EncodedCommand',base64.b64encode(code.encode('utf-16le')).decode()]
        result=subprocess.run(cmd,capture_output=True,timeout=45,check=True)
        return json.loads(result.stdout.decode('utf-8-sig'))
    async def read(self,data,name,depth=0,verified_ole=False):
        kind=media_classify(name)
        if verified_ole and data.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):kind=None
        if kind:return self.defer(name,len(data),kind)
        node=self.new_node(data,name);start=len(self.nodes);ext=Path(name).suffix.lower();self.progress('reading_'+ext)
        part_start=len(self.parts);cache_key=(node['sha256'],ext)
        if cache_key in self.cache:
            cached=self.cache[cache_key]
            node.update(status='complete',method='identical_content_read_reuse',reused_node_index=cached['node_index'])
            self.parts.extend(cached['parts']);return node
        try:
            if depth>20:raise ValueError('container_depth_requires_review')
            if ext in ACTIVE:raise ValueError('active_file_not_executed')
            if name.split('/')[-1].strip().startswith('._') and data[:4]==b'\x00\x05\x16\x07':
                node.update(method='appledouble_filesystem_metadata',metadata_only=True)
            elif ext in IMAGE:await self.image(data,node)
            elif ext=='.pdf' or data.startswith(b'%PDF-'):await self.pdf(data,node,depth)
            elif ext in {'.docx','.xlsx','.pptx','.xls','.doc','.ppt','.rtf'}:await self.office(data,node,ext,depth)
            elif data.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):await self.ole(data,node,depth)
            elif ext in {'.eml','.msg'}:
                if ext=='.msg':raise ValueError('outlook_binary_message_requires_reader')
                await self.eml(data,node,depth)
            elif ext=='.zip' or zipfile.is_zipfile(io.BytesIO(data)):
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    if any(p in z.namelist() for p in ('xl/workbook.xml','word/document.xml','ppt/presentation.xml')):
                        await self.office(data,node,ext,depth)
                        node['status']='incomplete' if any(n['status'] in {'incomplete','failed','reading','deferred_video','deferred_media'} for n in self.nodes[start:]) else 'complete'
                        return node
                    self.check_zip(z);node.update(method='archive_all_entries',entries_expected=sum(not x.is_dir() for x in z.infolist()),entries_read=0)
                    for info in z.infolist():
                        if info.is_dir():continue
                        child=await self.member(z,info,name,depth+1)
                        if child['status'] not in {'deferred_video','deferred_media'}:node['entries_read']+=1
            elif ext in TEXT or ext in {'.html','.htm'}:
                t=decode(data)
                if ext in {'.html','.htm'}:p=HTMLText();p.feed(t);t=''.join(p.parts)
                node['method']='full_text_decode';self.add(node,t)
            elif not data:node.update(method='verified_empty_file',no_text_detected=True)
            else:raise ValueError('unsupported_content_type')
            node['status']='incomplete' if any(n['status'] in {'incomplete','failed','reading','deferred_video','deferred_media'} for n in self.nodes[start:]) else 'complete'
            if node['status']=='complete':self.cache[cache_key]={'node_index':start-1,'parts':self.parts[part_start:]}
        except Exception as ex:
            node.update(status='failed',error_code=str(ex) if isinstance(ex,ValueError) else type(ex).__name__)
            # Private diagnostic stays on Windows; transport only stable error codes.
            with (self.job/'full-read-errors.log').open('a',encoding='utf-8') as f:
                import traceback;f.write('\n'+name+'\n');traceback.print_exc(file=f)
        return node
    async def run(self):
        d=json.loads((self.job/'download.json').read_text(encoding='utf-8-sig'));source=Path(d['remote_path'])
        if source.parent.resolve()!=self.job.resolve():raise ValueError('source_path_mismatch')
        if media_classify(source.name):raise ValueError('video_or_unknown_media_waiting_instruction')
        data=source.read_bytes()
        binding={'method':'same_source_bytes','raw_sha256':d['source_sha256'],'raw_bytes':d['source_bytes'],'view_sha256':sha(data),'view_bytes':len(data)}
        if sha(data)!=d['source_sha256'] or len(data)!=d['source_bytes']:
            original=self.raw_source_identity(source)
            if original.get('sha256')!=d['source_sha256'] or original.get('bytes')!=d['source_bytes']:raise ValueError('source_hash_mismatch')
            if sha(source.read_bytes())!=sha(data):raise ValueError('source_view_changed')
            binding['method']='windows_original_container_and_authorized_read_view'
        # The current authorized input is verified before touching shared products.
        cache_info={'mode':'off','hit':False,'avoided_parses':0}
        registry=None;event=None;lease=None;hit=None;profile=None
        control=d.get('content_cache_request')
        if control and control.get('mode') in {'observe','exact_reuse'}:
            ctx=control['context'];Registry.context(ctx)
            versions={}
            for package in ('Pillow','PyMuPDF','olefile','pywin32','winrt-Windows.Media.Ocr'):
                try:versions[package]=importlib.metadata.version(package)
                except importlib.metadata.PackageNotFoundError:versions[package]='unavailable'
            ocr={'model':'Windows.Media.Ocr','languages':[],'tile':3500,'overlap':128,'packages':versions,'platform':sys.platform}
            if os.name=='nt':
                ocr['os_build']=str(sys.getwindowsversion())
                from winrt.windows.media.ocr import OcrEngine
                ocr['languages']=sorted(x.language_tag for x in OcrEngine.available_recognizer_languages if x.language_tag.startswith(('en','zh')))
                ocr['max_dimension']=OcrEngine.max_image_dimension
            profile={'representation':'windows-authorized-read-view','format':source.suffix.lower(),
                'raw_sha256':d['source_sha256'],'raw_bytes':d['source_bytes'],'view_sha256':sha(data),'view_bytes':len(data),
                'parser_version':VERSION,'parser_sha256':key({'reader':sha(Path(__file__).read_bytes()),'identity':sha(Path(__import__('content_identity').__file__).read_bytes()),'environment':ocr}),
                'scope':control['scope'],'redaction':control['redaction'],'exclusions':control['exclusions'],
                'domain':ctx['domain'],'purpose':ctx['purpose'],'permission_epoch':ctx['permission_epoch'],'ocr':ocr,'policy_sha256':control['policy_sha256']}
            cache_info={'mode':control['mode'],'hit':False,'avoided_parses':0,'key':cache_key(profile),'profile':profile,'reason':'observing'}
            try:
                registry=Registry(self.job.parent.parent/'content-cache'/'registry.sqlite')
                event=registry.register(control['source'],control['event_id'],ctx,{'input_binding':binding,'source_path':str(source),'identity_sha256':d['identity_sha256']})
                # Encrypted raw containers remain observation-only until byte verification
                # of BOTH views is available. A matching clear view never bypasses that.
                eligible=control.get('eligible') is True and binding['method']=='same_source_bytes'
                if control['mode']=='exact_reuse' and eligible:
                    hit=registry.lookup(event,ctx,profile,data)
                    if not hit:lease=registry.claim(event,ctx,profile)
                    cache_info['reason']='miss' if lease else 'lease_busy'
                elif not eligible:cache_info['reason']='format_or_raw_view_not_enabled'
            except AccessDenied:raise
            except (OSError,sqlite3.Error):
                cache_info['reason']='index_unavailable';registry=None
        if hit:
            self.nodes=hit['product']['coverage']['nodes']
            text=hit['product']['text'];self.parts=[]
            cache_info.update(hit=True,avoided_parses=1,generation=hit['generation'],reason='verified_exact_bytes')
        else:
            await self.read(data,source.name)
            text=re.sub(r'\r\n?', '\n', '\n\n'.join(self.parts))
        if source.read_bytes()!=data:raise ValueError('source_view_changed')
        if binding['method']!='same_source_bytes':
            final_raw=self.raw_source_identity(source)
            if final_raw.get('sha256')!=d['source_sha256'] or final_raw.get('bytes')!=d['source_bytes']:raise ValueError('source_hash_mismatch')
        b=text.encode('utf-8');(self.job/'full-text.txt').write_bytes(b)
        complete=all(n['status'] in {'complete','excluded_self_sender'} for n in self.nodes)
        chunks=[{'index':i//24000,'chars':len(text[i:i+24000]),'sha256':sha(text[i:i+24000].encode())} for i in range(0,len(text),24000)]
        chunk_folder=self.job/'text-chunks';chunk_folder.mkdir(exist_ok=True)
        for item in chunks:
            offset=item['index']*24000;(chunk_folder/(str(item['index']).zfill(5)+'.txt')).write_bytes(text[offset:offset+24000].encode('utf-8'))
        result={**d,'schema_version':2,'reader_version':VERSION,'reader_sha256':sha(Path(__file__).read_bytes()),'host':socket.gethostname().upper(),'downloaded':True,'original_received_on_m2':False,'foreground_input_used':False,'clipboard_used':False,'local_view_sha256':sha(data),'source_binding':binding,'method':'windows_full_recursive_read','extract_status':'extracted' if complete else 'incomplete_content','risk':'ordinary' if complete else 'unknown','text':text,'text_chars':len(text.encode('utf-16le'))//2,'text_sha256':sha(b),'text_bytes':len(b),'coverage':{'complete':complete,'nodes':self.nodes,'content_truncated':False,'text_chunk_size':24000,'text_chunks':chunks,'scope':'full machine text extraction and available OCR; not business verification'},'completed_at':now()}
        result['video_policy']=VIDEO_POLICY
        result['content_cache']=cache_info
        if registry and event:
            product={k:result[k] for k in ('text','text_sha256','text_bytes','coverage')}
            if lease:
                try:
                    committed=registry.commit(event,ctx,profile,lease,product,{'path':str(source),'sha256':sha(data),'bytes':len(data)})
                    cache_info['committed']=committed
                    if committed:cache_info['generation']=lease['generation']
                except AccessDenied:raise
                except (OSError,sqlite3.Error,StaleWork):cache_info['committed']=False
            try:
                registry.receipt(event,ctx,'task_result',{'job_id':d['job_id'],'identity_sha256':d['identity_sha256'],'cache_key':cache_info.get('key'),'hit':cache_info['hit']})
            except AccessDenied:raise
            except (OSError,sqlite3.Error):cache_info['registry_receipt']='index_unavailable'
        atomic(self.job/'full-result.json',result)
        if registry and event:
            try:registry.acknowledge(event,ctx)
            except AccessDenied:raise
            except (OSError,sqlite3.Error):pass  # Durable outbox remains pending for recovery.
        self.progress('complete' if complete else 'incomplete');return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--job',required=True);a=p.parse_args()
    asyncio.run(Reader(a.job).run())
if __name__=='__main__':main()
