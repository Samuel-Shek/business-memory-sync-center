"""Bound, metadata-only recovery receipts; never calls models or writes memories."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

from pathlib import Path
from datetime import datetime, timedelta
import argparse
import hashlib
import json
import os
import re
import tempfile
import sqlite3
import stat

ROOT = Path(_archive_path('/Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync'))
PIPELINE = ROOT / '核对/.pipeline'
VERSION = 'minutes-recovery-20261003-v1'
DATABASE = Path(_archive_path('/Users/example/AI/LocalData/BUSINESS-NonObsidian.nosync/dingtalk-sync-v2/state/sync.sqlite'))
MEMORY_STATES = {'review_no_business_change','review_conflict_created','review_candidates_revised'}
STAGES = {'preflight', 'asr', 'memory_context', 'model_compare', 'result_validation'}
STATES = {'started', 'returned', 'completed', 'failed'}
CODES = {'asr_hotword_person_evidence_missing','asr_hotword_contract_invalid','', 'diagnostic_redacted', 'context_export_failed', 'authentication_invalid', 'login_required',
         'memory_core_busy', 'memory_core_unverified', 'maintenance_active', 'reference_binding_unverified',
         'checkpoint_unreadable', 'model_result_uncertain', 'existing_model_result_requires_validation',
         'same_version_recovery_already_attempted', 'checkpoint_source_changed', 'legacy_call_receipt_unverified',
         'reference_reuse_before_model', 'memory_context_empty', 'memory_root_unavailable', 'memory_root_permission_denied',
         'committed_review_receipt_recovered', 'memory_receipt_binding_mismatch'} | {'exit_'+str(i) for i in range(126)}

def safe_code(value):
    return value if isinstance(value, str) and value in CODES else 'diagnostic_redacted'

def journal_path(uuid):
    return ROOT / '核对/.state/recovery' / (digest(uuid)+'.json')

def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def file_hash(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

def read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}

def atomic(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('unsafe_receipt_path')
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def verified_asr_log_block(log, task_id, segment_count):
    """Dispatcher appends one dated block per execution, stdout then stderr."""
    try:
        chunks = re.split(r'(?m)^===== [^\r\n]+ =====\r?$', Path(log).read_text())[1:]
        matching = []
        for chunk in chunks:
            ids = re.findall(r'(?m)^任务 ID：[ \t]*([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})[ \t]*$',chunk)
            completed = re.findall(r'(?m)^参考转写完成：([0-9]+) 段，说话人分离 (?:有效|未生效)[ \t]*$',chunk)
            if task_id in ids:
                if ids != [task_id] or completed != [str(segment_count)] or not re.search(r'(?m)^已写入：参考稿\.json[ \t]*$',chunk):
                    return None
                matching.append(digest(chunk.strip()))
        return matching[0] if len(matching) == 1 else None
    except (OSError,ValueError,TypeError):
        return None

def authenticated_asr_reference(uuid, entry, meeting, raw, source):
    """Source-controller receipt of an existing task's verified input AND output.

    Sparse speech may differ in wording. This proves origin, never review quality.
    Missing, changed or unreadable proof does not authorize reuse.
    """
    receipt_path = meeting / '.asr-source-receipt.json'
    try:
        st = receipt_path.lstat()
        if receipt_path.is_symlink() or not receipt_path.is_file() or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
            return False
        receipt = read(receipt_path)
        audio_name = entry.get('media_filename')
        if not isinstance(audio_name,str) or Path(audio_name).name != audio_name:
            return False
        audio = ROOT / '音频' / audio_name
        if audio.is_symlink() or not audio.is_file():
            return False
        if (receipt.get('schema_version') != 1
                or receipt.get('method') != 'authenticated_existing_asr_input_and_result'
                or receipt.get('task_uuid') != uuid or receipt.get('provider_status') != 'SUCCEEDED'
                or receipt.get('input_bytes_verified') is not True
                or receipt.get('normalized_result_matches_reference') is not True
                or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', str(receipt.get('provider_task_id')))
                or not all(re.fullmatch('[a-f0-9]{64}',str(receipt.get(name))) for name in
                    ('provider_response_sha256','provider_input_sha256','local_preprocessed_sha256','preprocessor_sha256','asr_log_block_sha256'))
                or receipt['provider_input_sha256'] != receipt['local_preprocessed_sha256']
                or type(receipt.get('provider_input_bytes')) is not int or receipt['provider_input_bytes'] <= 44
                or receipt['provider_input_bytes'] != receipt.get('local_preprocessed_bytes')
                or receipt.get('binding') != {'basic':file_hash(raw/'basic.json'),
                    'transcript':file_hash(raw/'transcript.json'),'draft':file_hash(source),
                    'reference':file_hash(meeting/'参考稿.json'),'audio':file_hash(audio)}
                or receipt['binding']['audio'] != entry.get('media_sha256')
                or receipt.get('normalized_result_sha256') != digest(read(meeting/'参考稿.json'))):
            return False
        log = ROOT / '核对/.state/logs' / ('review-'+hashlib.sha256(uuid.encode()).hexdigest()[:16]+'.log')
        return verified_asr_log_block(log,receipt['provider_task_id'],len(read(meeting/'参考稿.json')['segments'])) == receipt['asr_log_block_sha256']
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        return False

def identity(uuid, entry):
    if not isinstance(uuid, str) or not uuid or '/' in uuid or '\\' in uuid or uuid in {'.', '..'}:
        return None
    filename = str(entry.get('filename') or '')
    if Path(filename).name != filename:
        return None
    stem = Path(str(entry.get('filename') or '')).stem
    meeting = ROOT / '核对' / stem
    raw = ROOT / '.raw' / uuid
    if (meeting.resolve().parent != (ROOT/'核对').resolve()
            or raw.resolve().parent != (ROOT/'.raw').resolve()
            or any(path.is_symlink() for path in (raw/'basic.json',raw/'transcript.json',meeting/'参考稿.json',meeting/(stem+'.md')))):
        return None
    basic = read(raw / 'basic.json')
    transcript = read(raw / 'transcript.json')
    reference = read(meeting / '参考稿.json')
    paragraphs = transcript.get('paragraphList')
    segments = reference.get('segments')
    if (not stem or basic.get('taskUuid') != uuid or transcript.get('taskUuid') != uuid or transcript.get('complete') is not True
            or not isinstance(paragraphs, list) or not paragraphs
            or transcript.get('paragraphCount') != len(paragraphs)
            or reference.get('engine') != 'qwen-audio-3.0-asr-flash-filetrans'
            or reference.get('language') != 'zh' or not isinstance(segments, list) or not segments
            or not all(isinstance(s, dict) and isinstance(s.get('begin_ms'), int)
                       and isinstance(s.get('end_ms'), int) and s['end_ms'] >= s['begin_ms']
                       and str(s.get('text') or '').strip() for s in segments)):
        return None
    # Current source must match the original draft supplied to the original ASR run.
    # Whitespace-normalized paragraph sequence is checked in order, without logging it.
    source = meeting / (stem + '.md')
    try:
        draft = source.read_text()
        if ('source_sha256: '+file_hash(raw/'transcript.json') not in draft
                or 'task_uuid: '+uuid not in draft):
            return None
        offset = 0
        for p in paragraphs:
            text = str(p.get('paragraph') or '').strip()
            if not text:
                return None
            index = draft.find(text, offset)
            if index < 0:
                return None
            offset = index + len(text)
    except OSError:
        return None
    def grams(text):
        text = re.sub(r'[^\w\u4e00-\u9fff]', '', text)
        return {text[i:i+4] for i in range(len(text)-3)}
    left = grams(''.join(p['paragraph'] for p in paragraphs))
    right = grams(''.join(s['text'] for s in segments))
    sparse = not left or not right or len(left & right) / min(len(left),len(right)) < 0.5
    if sparse and not authenticated_asr_reference(uuid,entry,meeting,raw,source):
        return None
    bound = {'task': digest(uuid), 'basic': file_hash(raw / 'basic.json'),
            'transcript': file_hash(raw / 'transcript.json'), 'reference': file_hash(meeting / '参考稿.json'),
            'draft': file_hash(source), 'audio': entry.get('media_sha256'),
            'code': {name: file_hash(PIPELINE / name) for name in ('review_dispatch.py', 'run_review.sh')},
            'helper': file_hash(__file__), 'policy': VERSION}
    if sparse:
        bound['asr_source_receipt'] = file_hash(meeting/'.asr-source-receipt.json')
    return bound

def qualification(uuid, entry, row):
    ident = identity(uuid, entry)
    meeting = ROOT / '核对' / Path(str(entry.get('filename') or '')).stem
    checkpoint = read(meeting / '.recovery-checkpoint.json')
    primary_path = journal_path(uuid)
    primary = read(primary_path)
    result = {'eligible': False, 'reason': 'reference_binding_unverified', 'key': None, 'identity': ident}
    if not ident:
        return result
    if primary_path.exists() and (not isinstance(primary, dict) or not primary):
        result['reason'] = 'checkpoint_unreadable'
        return result
    if ident.get('asr_source_receipt') and any(cp.get('source_verification_receipt',{}).get('receipt_sha256') != ident['asr_source_receipt'] for cp in (primary,checkpoint)):
        result['reason'] = 'checkpoint_source_changed'
        return result
    if primary.get('model_started'):
        reports = list((meeting / '核对结果').glob('*最终核对稿.md'))
        if (len(reports) == 1 and validated_review(uuid, entry, meeting)
                and committed_review(uuid, entry, reports[0], row)):
            result.update(eligible=True, reason='committed_review_receipt_recovered', key=digest(ident))
            return result
        result['reason'] = 'model_result_uncertain'
        return result
    if (meeting / '.recovery-checkpoint.json').exists() and (not isinstance(checkpoint, dict) or not checkpoint):
        result['reason'] = 'checkpoint_unreadable'
        return result
    key = digest(ident)
    result['key'] = key
    # A dispatch record can be left in-progress by process death. Check persisted
    # request state before considering any other condition, even after a code change.
    if checkpoint.get('model_started') and checkpoint.get('stage') != 'result_validation':
        result['reason'] = 'model_result_uncertain'
        return result
    if checkpoint.get('model_started'):
        result['reason'] = 'existing_model_result_requires_validation'
        return result
    if key in row.get('recovery_attempts', {}):
        result['reason'] = 'same_version_recovery_already_attempted'
        return result
    if checkpoint:
        if checkpoint.get('stage') not in {'preflight', 'asr', 'memory_context'}:
            return result
        if not checkpoint.get('binding') or checkpoint['binding'] != ident:
            result['reason'] = 'checkpoint_source_changed'
            return result
    else:
        # Legacy qualification is deliberately narrow: full reference and original
        # draft, historical exit 2 before context export, and no model artifacts.
        log = ROOT / '核对/.state/logs' / ('review-' + hashlib.sha256(uuid.encode()).hexdigest()[:16] + '.log')
        try:
            evidence = log.read_text()
        except OSError:
            evidence = ''
        if (not re.search(r'退出码 2(?:[，,\s]|$)',str(row.get('reason') or '')) or '参考转写完成' not in evidence
                or (meeting / (meeting.name+'.md')).stat().st_mtime > (meeting/'参考稿.json').stat().st_mtime
                or (meeting / '受治理记忆上下文.json').exists()
                or list((meeting / '核对结果').glob('.codex-*'))):
            result['reason'] = 'legacy_call_receipt_unverified'
            return result
        result['legacy_log_sha256'] = file_hash(log)
    result.update(eligible=True, reason='reference_reuse_before_model')
    return result

def review_artifacts(meeting):
    result = Path(meeting) / '核对结果'
    if result.is_symlink() or not result.is_dir():
        raise ValueError('review_artifacts_invalid')
    files = [p for p in result.iterdir() if p.is_file() and not p.name.startswith('.')]
    if any(p.is_symlink() or p.stat().st_size == 0 for p in files):
        raise ValueError('review_artifacts_invalid')
    artifacts = {p.name: file_hash(p) for p in files}
    if (not artifacts.get('核对运行元数据.json') or not artifacts.get('差异判断.json')
            or len([n for n in artifacts if n.endswith('最终核对稿.md')]) != 1):
        raise ValueError('review_artifacts_incomplete')
    return artifacts

def validated_review(uuid, entry, meeting):
    """Only a persisted successful validator event can authorize cached output."""
    ident = identity(uuid, entry)
    receipt = read(journal_path(uuid))
    if (not ident or not isinstance(receipt, dict) or receipt.get('binding') != ident
            or receipt.get('stage') != 'result_validation' or receipt.get('state') != 'completed'
            or receipt.get('model_started') is not True or not receipt.get('validated_artifacts')):
        return False
    try:
        return receipt['validated_artifacts'] == review_artifacts(meeting)
    except (OSError, ValueError):
        return False

def committed_review(uuid, entry, report, row):
    """Read an existing database receipt; this does not reconcile or write memory."""
    previous = row.get('memory_reconciliation_receipt')
    try:
        if previous is None:
            if not validated_review(uuid, entry, Path(report).parent.parent):
                return None
            with sqlite3.connect(DATABASE.as_uri()+'?mode=ro',uri=True,timeout=5) as connection:
                record = connection.execute('''SELECT q.status FROM recordings r JOIN review_reconciliations q
                    ON q.recording_id=r.recording_id WHERE r.task_uuid=? AND q.review_sha256=?''',
                    (uuid,file_hash(report))).fetchone()
            if not record or record[0] not in MEMORY_STATES:
                return None
            return memory_receipt(uuid,entry,report,{'status':'noop','reconciliation_status':record[0]})
        if not isinstance(previous, dict):
            return None
        return previous if memory_receipt(uuid, entry, report, previous) == previous else None
    except (OSError, ValueError, sqlite3.Error):
        return None

def hotword_contract(uuid, entry, meeting, preparing):
    """Exclude explicit source speaker labels before a NEW ASR; preserve legacy evidence."""
    raw = ROOT/'.raw'/uuid
    transcript = read(raw/'transcript.json')
    words_path = meeting/'热词词表.json'
    try:
        if words_path.is_symlink() or not words_path.is_file():raise ValueError('asr_hotword_contract_invalid')
        original_bytes=words_path.read_bytes();words=json.loads(original_bytes)
    except (OSError,ValueError):
        raise ValueError('asr_hotword_contract_invalid')
    paragraphs = transcript.get('paragraphList') if isinstance(transcript,dict) else None
    if (not isinstance(words,dict) or not isinstance(paragraphs,list) or transcript.get('taskUuid') != uuid
            or not all(isinstance(k,str) and type(v) in {int,float} for k,v in words.items())):
        raise ValueError('asr_hotword_contract_invalid')
    names=set()
    for paragraph in paragraphs:
        if not isinstance(paragraph,dict):continue
        for key in ('nickName','speakerDisplay'):
            label=paragraph.get(key)
            if isinstance(label,dict):label=label.get('nickName')
            if isinstance(label,str) and label.strip():names.add(label.strip().casefold())
    # Source display labels may combine a full Chinese name and an English name.
    # Conservatively omit vocabulary terms contained in those explicit labels.
    excluded=[k for k in words if k.strip().casefold() in names or
              (len(k.strip())>=2 and any(k.strip().casefold() in name for name in names))]
    binding = {'task':digest(uuid),'transcript':file_hash(raw/'transcript.json'),
               'draft':file_hash(meeting/(meeting.name+'.md')),'audio':entry.get('media_sha256'),
               'speaker_labels_sha256':digest(sorted(names))}
    receipt_path = meeting/'.asr-hotword-submission.json'
    if preparing:
        if (meeting/'参考稿.json').exists():
            raise ValueError('asr_hotword_contract_invalid')
        old_hash=hashlib.sha256(original_bytes).hexdigest()
        if excluded:
            history=ROOT/'核对/.state/recovery/hotwords';history.mkdir(mode=0o700,parents=True,exist_ok=True)
            fd,temp=tempfile.mkstemp(prefix='.hotword-',dir=history)
            with os.fdopen(fd,'wb') as stream:stream.write(original_bytes)
            os.chmod(temp,0o600);os.replace(temp,history/(old_hash+'.json'))
            atomic(words_path,{k:v for k,v in words.items() if k not in excluded})
        payload={'schema_version':1,'method':'source_speaker_labels_excluded_before_asr',
                 'state':'prepared','binding':binding,'original_words_sha256':old_hash,
                 'submitted_words_sha256':file_hash(words_path),'excluded_count':len(excluded),
                 'excluded_labels_sha256':digest(sorted(k.casefold() for k in excluded)),
                 'observed_at':now(),'asr_script_sha256':file_hash(Path.home()/'.agents/skills/transcription-review/scripts/transcribe_dashscope.py')}
        atomic(receipt_path,payload)
        return payload
    if excluded:
        raise ValueError('asr_hotword_person_evidence_missing')
    if receipt_path.exists():
        before=read(receipt_path)
        if (not isinstance(before,dict) or before.get('method')!='source_speaker_labels_excluded_before_asr'
                or before.get('binding')!=binding or before.get('submitted_words_sha256')!=file_hash(words_path)
                or before.get('state') not in {'prepared','completed'}):
            raise ValueError('asr_hotword_contract_invalid')
        current={**before,'state':'completed','reference_sha256':file_hash(meeting/'参考稿.json'),'completed_at':before.get('completed_at') or now()}
        atomic(receipt_path,current)
        return current
    return {'state':'legacy_without_submission_snapshot','person_terms_present':False}

def stage(meeting, name, state, error=''):
    if name not in STAGES or state not in STATES:
        raise ValueError('invalid_recovery_stage')
    error = safe_code(error)
    meeting = Path(meeting).resolve()
    if meeting.parent != (ROOT / '核对').resolve():
        raise ValueError('outside_review_root')
    path = meeting / '.recovery-checkpoint.json'
    payload = read(path)
    if path.exists() and (not isinstance(payload, dict) or not payload):
        raise ValueError('checkpoint_unreadable')
    matching = [(uuid, entry) for uuid,entry in read(ROOT/'.state/sync-state.json').get('entries',{}).items()
                if Path(str(entry.get('filename') or '')).stem == meeting.name]
    expected_uuid = os.environ.get('DINGTALK_REVIEW_TASK_UUID')
    if len(matching) != 1 or (expected_uuid and matching[0][0] != expected_uuid):
        raise ValueError('review_task_identity_ambiguous')
    uuid, entry = matching[0]
    hotword_failure = None
    if name=='asr' and state in {'started','completed'}:
        try:
            hotword_contract(uuid,entry,meeting,state=='started')
        except ValueError as exc:
            hotword_failure=str(exc)
            if hotword_failure not in {'asr_hotword_person_evidence_missing','asr_hotword_contract_invalid'}:
                raise
            state='failed';error=hotword_failure
    bound = identity(uuid, entry)
    if not bound and name not in {'preflight', 'asr'}:
        raise ValueError('review_source_binding_invalid')
    if payload.get('binding') and payload['binding'] != bound:
        raise ValueError('checkpoint_source_changed')
    allowed = {'schema_version','policy','stage','state','updated_at','safe_error_code','model_started',
               'model_call_started_at','asr_started','binding','events','validated_artifacts','source_verification_receipt'}
    payload = {k:v for k,v in payload.items() if k in allowed}
    payload.update(schema_version=1, policy=VERSION, stage=name, state=state, updated_at=now())
    # These receipts contain hashes/state only; shell errors cannot inject content.
    if error and re.fullmatch('[a-z0-9_]{1,100}', error):
        if not (error.startswith('exit_') and payload.get('safe_error_code') and not payload['safe_error_code'].startswith('exit_')):
            payload['safe_error_code'] = error
    elif state != 'failed':
        payload.pop('safe_error_code', None)
    if name == 'model_compare' and state == 'started':
        payload['model_started'] = True
        payload['model_call_started_at'] = now()
    if name == 'asr' and state == 'started':
        payload['asr_started'] = True
    if bound:
        payload['binding'] = bound
    if name == 'result_validation' and state == 'completed':
        payload['validated_artifacts'] = review_artifacts(meeting)
    payload['events'] = [event for event in payload.get('events',[]) if isinstance(event,dict)
                         and event.get('stage') in STAGES and event.get('state') in STATES and event.get('code') in CODES]
    payload.setdefault('events', []).append({'stage': name, 'state': state, 'at': now(), 'code': error})
    primary_path = journal_path(uuid)
    primary_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    prior = read(primary_path)
    if primary_path.exists() and (not isinstance(prior,dict) or not prior):
        raise ValueError('checkpoint_unreadable')
    if prior.get('model_started') and not payload.get('model_started'):
        raise ValueError('model_result_uncertain')
    # Stable task journal is written before the folder projection, so a rename
    # or later projection loss cannot erase an unknown request.
    atomic(primary_path, payload)
    atomic(path, payload)
    if hotword_failure:
        raise ValueError(hotword_failure)

def rebind_pre_model(uuid, entry, previous_identity, context_count, context_sha):
    """Authorized code release only: preserve request history before a model exists."""
    if not isinstance(context_count, int) or context_count <= 0 or not re.fullmatch('[a-f0-9]{64}', str(context_sha)):
        raise ValueError('current_context_receipt_invalid')
    current = identity(uuid, entry)
    comparable = lambda value: {k:v for k,v in value.items() if k not in {'code','helper'}}
    if (not current or not isinstance(previous_identity, dict) or current == previous_identity
            or comparable(current) != comparable(previous_identity)):
        raise ValueError('pre_model_source_changed')
    meeting = ROOT / '核对' / Path(entry['filename']).stem
    primary = journal_path(uuid); projection = meeting / '.recovery-checkpoint.json'
    before = read(primary)
    asr_events = [e for e in before.get('events', []) if e.get('stage') == 'asr']
    asr_completed = bool(asr_events and asr_events[-1].get('state') == 'completed')
    if (not before or before != read(projection) or before.get('binding') != previous_identity
            or before.get('stage') != 'memory_context' or before.get('state') != 'failed'
            or before.get('model_started') or (before.get('asr_started') and not asr_completed)
            or any(e.get('stage') == 'model_compare' for e in before.get('events', []))
            or list((meeting / '核对结果').glob('.codex-*'))):
        raise ValueError('pre_model_request_not_proven_absent')
    history = ROOT / '核对/.state/recovery/upgraded';history.mkdir(mode=0o700,parents=True,exist_ok=True)
    original_sha = file_hash(primary); atomic(history / (original_sha+'.json'), before)
    receipt = {'method':'pre_model_code_upgrade', 'previous_checkpoint_sha256':original_sha,
               'previous_identity_sha256':digest(previous_identity),'identity_sha256':digest(current),
               'memory_context_sha256':context_sha,'memory_count':context_count,'observed_at':now(),
               'model_requests_started':0,'asr_requests_started':0,'prior_asr_completed':asr_completed}
    # Existing failure events and counters remain; only the verified code binding
    # changes. Subsequent dispatch consumes the new version key once as usual.
    updated = {**before,'binding':current,'code_upgrade_receipt':receipt}
    atomic(primary, updated);atomic(projection, updated)
    return receipt

def context_access_probe(meeting, config_path=None):
    """Metadata only, under the actual caller; never reads note contents."""
    meeting = Path(meeting).resolve()
    if meeting.parent != (ROOT / '核对').resolve():
        raise ValueError('outside_review_root')
    config = read(config_path or Path(__file__).resolve().parents[1] / 'config.json')
    configured = config.get('memory_root')
    if not isinstance(configured, str) or not configured:
        raise ValueError('memory_root_unavailable')
    memory_root = Path(configured)
    folders = ['01_关键业务认知','02_内部业务资料','03_外部沟通','04_对话来源摘要','90_待确认']
    rows = []
    for ordinal, folder in enumerate(folders):
        try:
            with os.scandir(memory_root / folder) as iterator:
                has_entries = next(iterator, None) is not None
            rows.append({'ordinal':ordinal,'readable':True,'has_entries':has_entries})
        except OSError as exc:
            rows.append({'ordinal':ordinal,'readable':False,'safe_error_code':
                         'permission_denied' if exc.errno in {1,13} else 'missing' if exc.errno==2 else 'unavailable'})
    result = {'observed_at':now(),'source_root_hash':digest(str(memory_root)), 'folders':rows,
              'permission_denied':any(r.get('safe_error_code')=='permission_denied' for r in rows),
              'content_read':False,'models_started':0,'memory_writes':0}
    atomic(meeting / '.context-access-probe.json', result)
    return result

def failure_view(uuid, entry, row):
    meeting = ROOT / '核对' / Path(str(entry.get('filename') or '')).stem
    cp = read(meeting / '.recovery-checkpoint.json')
    q = qualification(uuid, entry, row)
    row_error = row.get('reason') if row.get('reason') in CODES else None
    code = safe_code(cp.get('safe_error_code') or row_error or q['reason'])
    reference_at = row.get('next_check_at')
    if not reference_at:
        try:
            observed = datetime.fromisoformat(cp.get('updated_at') or row.get('worker_finished_at') or row.get('updated_at'))
            reference_at = (observed + (timedelta(minutes=10) if q['eligible'] else timedelta(days=1))).isoformat(timespec='seconds')
        except (ValueError,TypeError):
            reference_at = now()
    return {'failure_step': 'memory_reconcile' if code == 'memory_receipt_binding_mismatch' else cp.get('stage') or 'legacy_context_export',
            'safe_error_code': code, 'recovery_eligible': q['eligible'],
            'recovery_condition': q['reason'], 'recovery_receipt_ref': str(meeting / '.recovery-checkpoint.json') if cp else None,
            'completed_artifacts': ['qwen_reference'] if q['identity'] else [],
            'next_check_at': reference_at,
            'manual_intervention_required': code in {'authentication_invalid', 'login_required'}}

def memory_receipt(uuid, entry, report, response):
    """Verify committed source-controller state independently of dispatcher fields."""
    if response.get('status') not in {'success','noop'}:
        raise ValueError('memory_receipt_incomplete')
    ident = identity(uuid,entry)
    report_sha = file_hash(report)
    if not ident or not report_sha:
        raise ValueError('memory_receipt_source_invalid')
    with sqlite3.connect(DATABASE.as_uri()+'?mode=ro',uri=True,timeout=5) as connection:
        connection.row_factory=sqlite3.Row
        row=connection.execute('''SELECT r.transcript_sha256,r.media_sha256,q.reconciliation_id,
              q.original_transcript_sha256,q.status FROM recordings r JOIN review_reconciliations q
              ON q.recording_id=r.recording_id WHERE r.task_uuid=? AND q.review_sha256=?''',(uuid,report_sha)).fetchone()
    if (not row or row['status'] not in MEMORY_STATES or response.get('reconciliation_status') != row['status']
            or row['transcript_sha256'] != ident['transcript'] or row['original_transcript_sha256'] != ident['transcript']
            or row['media_sha256'] != ident['audio']):
        raise ValueError('memory_receipt_binding_mismatch')
    payload = {'status':'success','reconciliation_status':row['status'],'report_sha256':report_sha,
               'transcript_sha256':row['transcript_sha256'],'reconciliation_id_hash':digest(row['reconciliation_id'])}
    payload['receipt_sha256']=digest(payload)
    return payload

def complete(uuid, entry, meeting, row):
    expected = ROOT / '核对' / Path(str(entry.get('filename') or '')).stem
    if Path(meeting).resolve() != expected.resolve():
        raise ValueError('completion_task_path_mismatch')
    ident = identity(uuid, entry)
    if not ident:
        raise ValueError('completion_source_binding_invalid')
    result = Path(meeting) / '核对结果'
    if result.resolve().parent != Path(meeting).resolve() or any(p.is_symlink() for p in result.iterdir() if p.is_file()):
        raise ValueError('completion_artifact_path_invalid')
    artifacts = {p.name: file_hash(p) for p in result.iterdir() if p.is_file() and not p.name.startswith('.')}
    if (not artifacts.get('核对运行元数据.json') or not artifacts.get('差异判断.json')
            or not any(name.endswith('最终核对稿.md') for name in artifacts)):
        raise ValueError('completion_artifacts_missing')
    reconciliation = row.get('memory_reconciliation_receipt')
    if (not isinstance(reconciliation,dict) or reconciliation.get('status') not in {'success','noop'}
            or reconciliation.get('reconciliation_status') != row.get('memory_reconciliation')
            or not reconciliation.get('report_sha256') or not reconciliation.get('receipt_sha256')):
        raise ValueError('completion_memory_receipt_missing')
    report_names=[name for name in artifacts if name.endswith('最终核对稿.md')]
    if len(report_names)!=1 or memory_receipt(uuid,entry,result/report_names[0],reconciliation) != reconciliation:
        raise ValueError('completion_memory_receipt_changed')
    receipt = {'schema_version': 1, 'policy': VERSION, 'status': 'completed', 'identity': ident,
               'artifacts': artifacts, 'memory_reconciliation': row.get('memory_reconciliation'),
               'memory_receipt': reconciliation, 'completed_at': now()}
    path = Path(meeting) / '.recovery-completion.json'
    previous = read(path)
    if previous and all(previous.get(key)==receipt.get(key) for key in ('identity','artifacts','memory_receipt','memory_reconciliation')):
        return str(path)
    if previous:
        history=ROOT/'核对/.state/recovery/completed';history.mkdir(mode=0o700,parents=True,exist_ok=True)
        atomic(history/(file_hash(path)+'.json'),previous)
    atomic(path, receipt)
    return str(path)

def accepted(uuid, entry, row):
    expected = ROOT / '核对' / Path(str(entry.get('filename') or '')).stem / '.recovery-completion.json'
    if not row.get('completion_receipt') or Path(row['completion_receipt']) != expected or expected.is_symlink():
        return None
    receipt = read(expected)
    if not isinstance(receipt, dict):
        return None
    ident = identity(uuid, entry)
    if (row.get('status') != 'done' or receipt.get('status') != 'completed'
            or not ident or receipt.get('schema_version') != 1 or receipt.get('policy') != VERSION
            or receipt.get('identity') != ident
            or receipt.get('memory_receipt') != row.get('memory_reconciliation_receipt')
            or not isinstance(receipt.get('memory_receipt'),dict)
            or receipt['memory_receipt'].get('status') not in {'success','noop'}
            or receipt.get('memory_reconciliation') != row.get('memory_reconciliation')):
        return None
    result = ROOT / '核对' / Path(entry['filename']).stem / '核对结果'
    if result.resolve().parent != expected.parent.resolve():
        return None
    artifacts = receipt.get('artifacts')
    if (not isinstance(artifacts, dict) or not artifacts.get('核对运行元数据.json')
            or not artifacts.get('差异判断.json') or not any(name.endswith('最终核对稿.md') for name in artifacts)
            or any(not isinstance(name, str) or Path(name).name != name or not isinstance(value, str)
                   or not re.fullmatch('[a-f0-9]{64}', value) or (result/name).is_symlink() or file_hash(result / name) != value
                   for name, value in artifacts.items())):
        return None
    try:
        reports=[name for name in artifacts if name.endswith('最终核对稿.md')]
        if len(reports)!=1 or memory_receipt(uuid,entry,result/reports[0],receipt['memory_receipt']) != receipt['memory_receipt']:
            return None
    except (OSError,ValueError,sqlite3.Error):
        return None
    return {'receipt_sha256': file_hash(row['completion_receipt']), 'policy': VERSION,
            'reviewed': True, 'memory_reconciliation': row.get('memory_reconciliation')}

def unbound_readiness_monitor(old, uuid):
    """A first-attempt ASR readiness observation has no historical source verdict."""
    try:
        obs=json.loads(old['observation_json']);ref=json.loads(old['source_ref_json'])
        if not isinstance(obs,dict) or not isinstance(ref,dict):return False
        return (old['source_id']=='dingtalk_minutes' and old['kind']=='review_recovery'
                and old['object_key']=='minutes-review:'+digest(uuid) and ref.get('task_uuid')==uuid
                and 'source_version' in obs and obs['source_version'] is None
                and obs.get('error_code')=='review_recovery_pending'
                and obs.get('failure_step')=='asr' and obs.get('safe_error_code')=='reference_binding_unverified'
                and type(obs.get('attempt_count')) is int and obs['attempt_count']==1
                and type(obs.get('recovery_attempt_count')) is int and obs['recovery_attempt_count']==0
                and obs.get('completed_artifacts')==[] and obs.get('manual_intervention_required') is False)
    except (KeyError,IndexError,TypeError,ValueError):
        return False

def healthy_first_asr_progress(row, checkpoint, stamp):
    try:
        age=(datetime.fromisoformat(stamp)-datetime.fromisoformat(row['heartbeat_at'])).total_seconds()
        return (row.get('status')=='in_progress' and type(row.get('attempts')) is int and row['attempts']==1
                and not row.get('original_failure') and not row.get('recovery_attempts')
                and checkpoint.get('stage')=='asr' and checkpoint.get('state')=='started'
                and not checkpoint.get('model_started') and not checkpoint.get('safe_error_code') and 0<=age<=90)
    except (KeyError,TypeError,ValueError):
        return False

def refresh_workbench(connection, stamp, new_item, upsert, finish, state_path=None):
    sync = read(state_path or ROOT / '.state/sync-state.json').get('entries', {})
    if not sync:
        return
    ledger = read(ROOT / '核对/.state/review-ledger.json')
    for uuid, row in ledger.items():
        entry = sync.get(uuid)
        if not isinstance(entry, dict) or not isinstance(row, dict):
            continue
        key = 'minutes-review:' + digest(uuid)
        prior = connection.execute("SELECT * FROM operational_issues WHERE object_key=? AND status='open'", (key,)).fetchall()
        proof = accepted(uuid, entry, row) if prior else None
        if proof:
            current_identity=identity(uuid,entry)
            current_source=digest({key:current_identity[key] for key in ('task','basic','transcript','reference','draft','audio')})
            for old in prior:
                old_observation=json.loads(old['observation_json'])
                if old_observation.get('source_version') == current_source:
                    finish(connection, old['issue_id'], 'source_receipt_verified', proof, stamp)
                elif unbound_readiness_monitor(old,uuid):
                    finish(connection, old['issue_id'], 'source_receipt_verified',
                           {**proof,'closure_scope':'current_verified_readiness','source_version':current_source,
                            'historical_source_version_missing':True}, stamp)
            continue
        if row.get('status') not in {'failed', 'interrupted', 'in_progress', 'review_done_reconcile_failed', 'review_done_reconcile_deferred'}:
            continue
        if entry.get('video_waiting_instruction') or row.get('review_required') is False:
            continue
        checkpoint=read(journal_path(uuid))
        if not prior and healthy_first_asr_progress(row,checkpoint,stamp):
            # Ordinary progress is already visible in the action/index; it is
            # not a persistent recovery failure with an invented source version.
            continue
        q = qualification(uuid, entry, row)
        obs = failure_view(uuid, entry, row)
        source_version=digest({key:q['identity'][key] for key in ('task','basic','transcript','reference','draft','audio')}) if q['identity'] else None
        obs.update(error_code='review_recovery_pending', reason_code=obs['safe_error_code'],
                   source_version=source_version,
                   attempt_count=row.get('attempts', 0), recovery_attempt_count=len(row.get('recovery_attempts', {})),
                   impact='核对尚未完成；已保存的文字与参考转写继续保留。',
                   recovery_boundary='复用既有转写；结果不明先查凭据，只有同条来源完成凭据才能关闭。',
                   next_due=obs['next_check_at'])
        matching=[old for old in prior if json.loads(old['observation_json']).get('source_version')==source_version]
        fingerprint = matching[0]['fingerprint'] if matching else digest([source_version, row.get('worker_started_at')])
        item = new_item('review_recovery', key, fingerprint, 'dingtalk_minutes', '听记核对自动恢复', obs,
                        {'task_uuid': uuid}, attention='needs_user' if obs['manual_intervention_required'] else 'automatic')
        upsert(connection, item, [], stamp)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['stage','context-probe'])
    parser.add_argument('meeting')
    parser.add_argument('name', nargs='?')
    parser.add_argument('state', nargs='?')
    parser.add_argument('error', nargs='?', default='')
    args = parser.parse_args()
    if args.command == 'context-probe':
        result = context_access_probe(args.meeting)
        print('memory_root_permission_denied' if result['permission_denied'] else 'memory_context_empty')
    else:
        if args.name is None or args.state is None:parser.error('stage requires name and state')
        stage(args.meeting, args.name, args.state, args.error)
