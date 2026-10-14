"""Source metadata and explicit single-object acquisition; no metadata-time downloads."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import base64,hashlib,json,os,sqlite3,subprocess,shutil,re,tempfile
from pathlib import Path
from datetime import datetime
import video_hold
ROOT=video_hold.ROOT
NAMES={'dingtalk_chat':'钉钉聊天','dingtalk_mail':'钉钉邮件','dingtalk_minutes':'钉钉 AI 听记','win11':'Win11','wechat':'微信'}
class SourceError(RuntimeError):
    def __init__(self, message, code='media_source_unavailable'):
        self.code=code
        super().__init__(message)

def config_value(config=None):return config if config is not None else json.loads((ROOT/'config.json').read_text())
def query(path,sql,args=()):
    if not Path(path).is_file():return None
    with sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        r=c.execute(sql,args).fetchone();return dict(r) if r else None

def resolve(h,config=None,*,mail=None,classifications=None):
    cfg=config_value(config);ref=json.loads(h.get('object_ref','{}'));source=h['source_id']
    if h.get('missing'):raise SourceError('原媒体登记已缺失，无法确认具体对象；可忽略此提醒。')
    if source=='dingtalk_chat':
        p=Path(cfg['external_root'])/'dingtalk-chat/state/chat.sqlite'
        row=query(p,'SELECT m.*,c.conversation_name AS conversation_title FROM messages m LEFT JOIN conversations c USING(conversation_id) WHERE message_id=?',(ref.get('message_id'),))
        if not row:raise SourceError('原消息记录暂不可用。')
        if row['resource_refs_sha256']!=h['version']:raise SourceError('原消息资源已变化，此处保留旧版本，请等待新版本登记。')
        refs=json.loads(row['resource_refs_json']);index=ref.get('resource_index')
        if not isinstance(index,int) or not 0<=index<len(refs):raise SourceError('原消息的资源关联缺失。')
        resource=refs[index];download=resource.get('download',{})
        args=dict(download.get('arguments',{}))
        import media_classification
        classified=(classifications if classifications is not None else media_classification.records()).get(h['hold_id'],{})
        evidence=json.loads(classified.get('evidence_json','{}'))
        if classified.get('version')==h['version'] and classified.get('state')=='identified' and evidence.get('original_resource_sha256')==video_hold.digest(resource) and evidence.get('download_resource_type')==resource.get('type'):
            args['resource-id']=evidence['download_resource_id']
        ready=download.get('ready') is True and download.get('shortcut')=='+messages-resource-download'
        return {'mode':'chat','resource':resource,'args':args,'ready':ready,'name':resource.get('name') or resource.get('fileName'),
            'parent':row.get('conversation_title') or '原会话','observed_at':row['create_time'],'sender':row.get('sender_name','')}
    if source=='dingtalk_minutes':
        root=Path(_archive_path('/Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync'))
        entry=json.loads((root/'.state/sync-state.json').read_text()).get('entries',{}).get(ref.get('task_uuid'))
        if not entry:raise SourceError('原听记记录暂不可用。')
        if entry.get('media_sha256')!=h['version']:raise SourceError('听记媒体版本已变化，请等待新版本登记。')
        paths=[]
        if entry.get('media_storage')=='pssd':
            rel=entry.get('media_archive_relative_path','')
            if not rel or Path(rel).is_absolute() or '..' in Path(rel).parts:raise SourceError('归档路径无效。')
            # Verify volume UUID before trusting a mount with a matching path.
            import plistlib
            for vol in Path('/Volumes').iterdir():
                candidate=vol/rel
                if not candidate.is_file():continue
                r=subprocess.run(['/usr/sbin/diskutil','info','-plist',str(vol)],capture_output=True,timeout=10)
                if r.returncode==0 and plistlib.loads(r.stdout).get('VolumeUUID','').upper()==str(entry.get('media_archive_volume_uuid','')).upper():paths.append(candidate)
        else:
            name=entry.get('media_filename','')
            if Path(name).name!=name:raise SourceError('媒体文件名无效。')
            for directory in ['原始媒体','media','.media']:
                if (root/directory/name).is_file():paths.append(root/directory/name)
        return {'mode':'local','path':str(paths[0]) if len(paths)==1 else None,'ready':len(paths)==1,
            'reason':'原始视频保存在外接归档盘，当前未连接；连接后可处理。' if not paths else '',
            'name':entry.get('media_filename'),'parent':entry.get('title'),'expected_sha':h['version'],
            'observed_at':datetime.fromtimestamp(entry['start_time']/1000).astimezone().isoformat() if entry.get('start_time') else None}
    if source=='dingtalk_mail':
        import operational_workbench as w
        mail=mail if mail is not None else w._mail(cfg)
        obj=next((a for a in mail['attachments'] if a['message_key']==ref.get('message_key') and a['attachment_id']==ref.get('attachment_id')),None)
        if not obj:raise SourceError('原邮件附件记录暂不可用。')
        if obj.get('sha256') and obj['sha256']!=h['version']:raise SourceError('所属附件版本已经变化，请等待新版本登记。')
        archive=Path(obj['archive_path']);manifest=json.loads((archive/'manifest.json').read_text())
        item=next(a for a in manifest['attachments'] if a.get('attachment_id_hash')==ref.get('attachment_id'))
        receipt_path=item.get('receipt_path')
        receipt=json.loads((archive/receipt_path).read_text()) if receipt_path else {}
        expected={x['path']:x['sha256'] for x in manifest.get('files',[])}
        if receipt_path and (hashlib.sha256((archive/receipt_path).read_bytes()).hexdigest()!=expected.get(receipt_path)):
            raise SourceError('附件原始回执校验失败。')
        message=next((m for m in mail['messages'] if m['message_key']==ref.get('message_key')), {})
        if item.get('storage')=='video_hold':
            return {'mode':'mail_download','ready':True,'name':h.get('name'),'parent':obj.get('original_name'),
                'message_key':ref['message_key'],'attachment_id':ref['attachment_id'],'observed_at':message.get('received_at'),
                'subject':message.get('subject'),'expected_sha':None}
        mode='remote_mail' if str(obj.get('stored_name','')).startswith('win11/') else 'local' 
        return {'mode':mode,'path':receipt.get('remote_path') if mode=='remote_mail' else str(archive/obj['stored_name']),
            'ready':bool(receipt.get('remote_path')) if mode=='remote_mail' else (archive/obj['stored_name']).is_file(),
            'member':ref.get('member'),'expected_sha':obj.get('sha256'),'view_sha':receipt.get('local_view_sha256'),
            'name':h.get('name'),'parent':obj.get('original_name'),'subject':message.get('subject'),'observed_at':message.get('received_at'),'receipt':receipt}
    if source=='win11':
        source_cfg=next((x for x in cfg.get('watch_sources',[]) if x.get('source_id')==ref.get('source_id')),None)
        rel=ref.get('relative_path')
        if not source_cfg or not rel:raise SourceError('此 Win11 媒体缺少可校验的原件位置；可忽略此提醒。')
        import folder_watcher
        if not folder_watcher.mount_is_expected(source_cfg):raise SourceError('Win11 原件共享盘未连接或来源校验未通过。')
        root=Path(source_cfg['root']).resolve();path=root/rel
        if Path(rel).is_absolute() or '..' in Path(rel).parts or path.is_symlink() or not path.resolve().is_relative_to(root):raise SourceError('Win11 原件路径无效。')
        stat=path.stat()
        if video_hold.digest([stat.st_size,stat.st_mtime_ns])!=h['version']:raise SourceError('Win11 原件版本已经变化，请等待新版本登记。')
        return {'mode':'local','path':str(path),'ready':True,'name':path.name,'parent':str(Path(rel).parent),
            'stat_version':h['version'],'expected_sha':None,'observed_at':datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()}

    if source=='wechat':
        root=Path(cfg['external_root'])/'wechat-chat'
        row=query(root/'state/chat.sqlite','SELECT * FROM media_assets WHERE asset_id=?',(ref.get('asset_id'),))
        if not row:raise SourceError('此微信媒体原始登记暂不可用。')
        if row.get('content_sha256') and row['content_sha256']!=h['version']:raise SourceError('微信媒体版本已经变化。')
        path=Path(row['local_path']) if row.get('local_path') else None
        if not path or not path.is_file():raise SourceError('微信尚未保存可校验的媒体原件；保存原件后重试。')
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():raise SourceError('微信媒体原件路径校验失败。')
        return {'mode':'local','path':str(path),'ready':True,'name':path.name,'parent':'已归档微信媒体','expected_sha':row['content_sha256']}

    raise SourceError('媒体来源暂不支持定位。')

def describe(h,config=None,*,mail=None,classifications=None):
    s=NAMES.get(h['source_id'],h['source_id']);name=h.get('name') or ''
    result={'available':False,'unavailable_reason':'','source_description':s,'observed_at':None,'source_binding':h.get('version',''),'original_name':name}
    try:
        info=resolve(h,config,mail=mail,classifications=classifications);result['available']=info['ready'];result['unavailable_reason']=info.get('reason','') if not info['ready'] else ''
        if not info['ready'] and not result['unavailable_reason']:result['unavailable_reason']='原件下载所需信息不完整。'
        name=info.get('name') or name
        result.update(observed_at=info.get('observed_at'),source_description=' · '.join(x for x in [s,info.get('subject'),info.get('parent'),info.get('sender')] if x))
        result['source_binding']=video_hold.digest({k:v for k,v in info.items() if k not in {'receipt','path'}})
    except (OSError,ValueError,KeyError,sqlite3.Error,SourceError) as exc:
        result['unavailable_reason']=str(exc) if isinstance(exc,SourceError) else '原始来源信息暂不可用。'
    # Reversible repair of cp1252-rendered GBK filenames; keep the exact original value.
    try:
        if any(ord(ch)>127 for ch in name) and not any('\u4e00'<=ch<='\u9fff' for ch in name):
            repaired=name.encode('cp1252').decode('gbk')
            if any('\u4e00'<=ch<='\u9fff' for ch in repaired) and repaired.encode('gbk').decode('cp1252')==name:
                name=repaired;result['name_recovered']=True
    except UnicodeError:pass
    if name in {'消息媒体','视频','附件',''}:
        at=result.get('observed_at') or h.get('first_seen','')
        ref=json.loads(h.get('object_ref','{}'));ordinal=ref.get('resource_index',0)+1
        name=f"{s} · {str(at)[:19].replace('T',' ')} · 第 {ordinal} 个媒体" if not h.get('missing') else f"{s} · 来源信息缺失"
    result['display_name']=name
    result['kind_label']='来源信息缺失' if h.get('missing') else '视频' if h.get('kind')=='video' else '媒体类型待确认'
    return result

def acquire(h,cfg,destination):
    info=resolve(h,cfg)
    if not info['ready']:raise SourceError(info.get('reason') or '原件信息不足，未下载。')
    destination.mkdir(parents=True,exist_ok=True);destination.chmod(0o700)
    from media_extract import member,sha,LIMIT
    if info['mode']=='chat':
        args=info['args'];allowed={'resource-id','type','message-id','open-conversation-id'}
        if set(args)-allowed or args.get('type') not in {'mediaId','fileId'} or not args.get('resource-id'):raise SourceError('资源下载参数不完整。')
        files_dir=Path(tempfile.mkdtemp(prefix='download-',dir=destination))
        cmd=[str(cfg.get('dingtalk_chat',{}).get('dws_bin') or _archive_path('/Users/example/.local/bin/dws')),'chat','+messages-resource-download','--format','json','--output',files_dir.name+'/']
        for k,v in args.items():
            if not isinstance(v,str) or len(v)>4096:raise SourceError('资源标识无效。')
            cmd += ['--'+k,v]
        before=set()
        try:r=subprocess.run(cmd,cwd=destination,capture_output=True,timeout=180)
        except subprocess.TimeoutExpired:raise SourceError('下载超时，原件状态待核对。','media_download_timeout')
        if r.returncode:
            # Keep structured diagnostics only; never persist signed URLs or private output.
            combined=(r.stdout+r.stderr).decode('utf-8','replace').lower()
            denied=bool(re.search(r'(?<![0-9])(401|403)(?![0-9])|accessdenied|permission.denied|forbidden|unauthorized',combined))
            raise SourceError('钉钉拒绝下载，等待来源条件恢复。' if denied else '钉钉资源下载未完成，系统有限重试。',
                              'media_download_denied' if denied else 'media_download_failed')
        files=[p for p in files_dir.rglob('*') if p.is_file() and not p.is_symlink() and p not in before]
        if len(files)!=1:raise SourceError('下载未返回唯一原件，结果未确认。')
        path=files[0]
        if path.stat().st_size>LIMIT:raise SourceError('媒体超过本次安全处理上限。')
        data=path.read_bytes()
    elif info['mode']=='mail_download':
        import email_sync as mail,win11_mail_attachments as win
        from urllib.parse import urlsplit
        import urllib.request
        safety=mail.read_safety_status(cfg)
        if not safety.get('server_side_pass') or not safety.get('cross_device_confirmed'):raise SourceError('邮件只读条件尚未核验，未下载。')
        with mail.connect_database_readonly(cfg) as c:
            message=dict(c.execute('SELECT * FROM messages WHERE message_key=?',(info['message_key'],)).fetchone())
        value=win.policy(cfg)
        remote=mail.run_dws(cfg,['mail','message','get','--email',message['mailbox'],'--id',message['message_id']]).get('message',{})
        if remote.get('id')!=message['message_id'] or win.sender_is_self(remote,value):raise SourceError('邮件来源身份或发件人规则不满足处理条件。')
        listed=mail.run_dws(cfg,['mail','attachment','list','--email',message['mailbox'],'--id',message['message_id']])
        matches=[x for x in listed.get('attachments',[]) if mail.sha256_text(str(x.get('id','')))[:16]==info['attachment_id'] and x.get('name')==h['name']]
        if len(matches)!=1:raise SourceError('视频附件无法唯一核对。')
        item=matches[0]
        if video_hold.digest([item['name'],mail.normalize_int(item.get('size'))])!=h['version']:raise SourceError('视频附件版本已变化，请等待新版本登记。')
        helper=win._checked_file(value,'session_helper')
        r=subprocess.run([str(helper),'mail','attachment','download','--email',message['mailbox'],'--message-id',message['message_id'],
            '--attachment-id',str(item['id']),'--name',item['name'],'--session-only','--format','json'],capture_output=True,timeout=90)
        session=json.loads(r.stdout) if r.returncode==0 else {}
        url=session.get('downloadUrl','');parts=urlsplit(url)
        if session.get('attachment_body_requested_on_mac') is not False or parts.scheme!='https' or parts.hostname not in win.ORIGINS:raise SourceError('附件下载来源校验失败。')
        class SafeRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,req,fp,code,msg,headers,newurl):
                dest=urlsplit(newurl)
                if dest.scheme!='https' or dest.hostname not in win.ORIGINS:raise SourceError('附件重定向来源不在允许范围。')
                return super().redirect_request(req,fp,code,msg,headers,newurl)
        with urllib.request.build_opener(SafeRedirect()).open(url,timeout=90) as response:data=response.read(LIMIT+1)
        if h.get('size_bytes') and len(data)!=h['size_bytes']:raise SourceError('视频附件实际大小与登记不一致。')
    elif info['mode']=='remote_mail':
        import win11_mail_attachments as win
        remote=str(info['path']).replace('\\','/')
        if not re.fullmatch(r'C:/Users/example/AppData/Local/BusinessMemory/mail-attachments/jobs/[0-9a-f]{32}/original\.[A-Za-z0-9]+',remote):raise SourceError('Win11 原件路径校验失败。')
        payload={'path':remote,'expected':info['expected_sha'],'view':info['view_sha'],'member':info['member']}
        # Runs only for this explicit decision; emits only selected media bytes, never the parent Office file.
        script=Path(__file__).with_name('media_extract.py').read_text()+'''\nimport json,base64,sys,pathlib,socket\nassert socket.gethostname().upper()=='EXAMPLE_USER'\np=json.loads(base64.b64decode(sys.argv[1]));path=pathlib.Path(p['path']);data=path.read_bytes()\nif sha(data) not in {p['expected'],p['view']}:raise ValueError('source_changed')\nresult=member(data,p['member']) if p['member'] else data\nif sha(path.read_bytes())!=sha(data):raise ValueError('source_changed_during_read')\nprint(base64.b64encode(result).decode())\n'''
        encoded=base64.b64encode(script.encode()).decode();arg=base64.b64encode(json.dumps(payload).encode()).decode()
        runner="& 'C:/Users/example/AppData/Local/BusinessMemory/mail-attachments/runtime/Scripts/python.exe' -c \"import base64;exec(base64.b64decode('"+encoded+"'))\" '"+arg+"';if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}"
        raw=win._ssh(win.policy(cfg)['ssh_host'],runner,timeout=180)
        try:data=base64.b64decode(raw.strip(),validate=True)
        except ValueError:raise SourceError('Win11 未返回有效媒体，原问题保持未完成。')
    else:
        path=Path(info['path'])
        if path.is_symlink() or path.stat().st_size>LIMIT:raise SourceError('原件路径或大小无法通过校验。')
        data=path.read_bytes()
        if info.get('stat_version') and video_hold.digest([path.stat().st_size,path.stat().st_mtime_ns])!=info['stat_version']:raise SourceError('原件版本已经变化。')
        if info.get('expected_sha') and sha(data)!=info['expected_sha']:raise SourceError('原件内容已变化，旧选择不能用于新版本。')
        if info.get('member'):data=member(data,info['member'])
        before_sha=sha(data) if not info.get('member') else info['expected_sha']
        if sha(path.read_bytes())!=before_sha:raise SourceError('原件在读取期间变化。')
    if len(data)>LIMIT:raise SourceError('媒体超过本次安全处理上限。')
    target=destination/'original.media';target.write_bytes(data);target.chmod(0o600)
    return target,{'source_sha256':sha(data),'source_bytes':len(data),'hold_id':h['hold_id'],'version':h['version'],'source_id':h['source_id'],'member':info.get('member')}
