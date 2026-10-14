"""Bounded, resumable model input; immutable extracted originals stay in archive."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import copy
import hashlib
import json
import uuid

FILE_APPROVAL_CHARS = 10_000_000
REQUEST_BUDGET = 800_000
CHUNK_CHARS = 500_000
VERSION = 1


class BatchInputError(RuntimeError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def serialized_size(document):
    return len(json.dumps(document, ensure_ascii=False, separators=(',', ':')))


def require_file_permission(text, name):
    if len(text) > FILE_APPROVAL_CHARS:
        raise BatchInputError('attachment_processing_approval_required',
                              f'{name}: {len(text)} characters exceeds {FILE_APPROVAL_CHARS}')


def text_chunks(text, maximum=CHUNK_CHARS):
    start = 0
    while start < len(text):
        end = min(start + maximum, len(text))
        if end < len(text):
            newline = text.rfind('\n', start + maximum // 2, end)
            if newline >= start:
                end = newline + 1
        yield start, end, text[start:end]
        start = end


def prepare(document, directory, invoke, validate, write_json, read_json, input_sha, contract_sha=''):
    """Use the same proposal model for all chunks, then reconcile its proposals.

    Chunk calls never apply business writes. Completion is recorded only after all
    ranges have cached, validated output. Cache identity includes context so that
    an interrupted run cannot mix proposals from different memory snapshots.
    """
    for message in document['messages']:
        for attachment in message.get('attachments', []):
            require_file_permission(attachment.get('extracted_text', ''), attachment.get('name', 'attachment'))
    if serialized_size(document) <= REQUEST_BUDGET:
        return document

    context = copy.deepcopy(document)
    for message in context['messages']:
        for attachment in message.get('attachments', []):
            attachment['extracted_text'] = ''
    if serialized_size(context) > REQUEST_BUDGET // 2:
        raise BatchInputError('batch_context_too_large', 'Non-attachment context needs separate partitioning')
    context_key = sha(json.dumps(context, ensure_ascii=False, sort_keys=True) + contract_sha)
    reduced = copy.deepcopy(context)
    coverage = {'schema_version': VERSION, 'status': 'running', 'input_sha256': input_sha(document),
                'context_sha256': context_key, 'files': []}
    write_json(directory / 'batch-coverage.json', coverage)

    for mi, message in enumerate(document['messages']):
        for ai, attachment in enumerate(message.get('attachments', [])):
            text = attachment.get('extracted_text', '')
            target = reduced['messages'][mi]['attachments'][ai]
            # Small files stay verbatim in the final proposal; they need no extra call.
            if len(text) < 30_000:
                target['extracted_text'] = text
                continue
            file_hash = sha(text)
            file_record = {'message_index': mi, 'attachment_index': ai, 'chars': len(text),
                           'text_sha256': file_hash, 'chunks': [], 'complete': False}
            coverage['files'].append(file_record)
            proposals = []
            for index, (start, end, chunk) in enumerate(text_chunks(text)):
                child = copy.deepcopy(context)
                child['messages'][mi]['attachments'][ai]['extracted_text'] = chunk
                # Other attachment texts are intentionally not part of this chunk.
                for cm, child_message in enumerate(child['messages']):
                    for ca, child_attachment in enumerate(child_message.get('attachments', [])):
                        if (cm, ca) != (mi, ai):
                            child_attachment['extract_status'] = 'not_in_current_chunk'
                            child_attachment['full_read_confirmed'] = False
                child['batch_processing'] = {
                    'mode': 'one_fragment_only', 'source_token': message['source_token'],
                    'attachment_index': ai, 'start_char': start, 'end_char': end,
                    'total_chars': len(text), 'text_sha256': file_hash,
                    'instruction': '仅分析本片段及邮件正文。保留表格、行号和时间范围；'
                                   '不把片段当全文，不外推数据，不将片段统计当总计。'
                                   '运行数据只归纳可证实的范围和内容，勿臆造故障或业务结论。'
                                   '所有提案仅供后续合并，不能代表业务写入或正式确认。'
                }
                if serialized_size(child) > REQUEST_BUDGET:
                    raise BatchInputError('batch_fragment_too_large', 'Serialized fragment exceeds request budget')
                cache_key = sha(json.dumps(child, ensure_ascii=False, sort_keys=True) + f'v{VERSION}' + contract_sha)
                child_id = str(uuid.uuid5(uuid.NAMESPACE_URL, cache_key))
                cache_path = directory / 'fragments' / f'{cache_key}.json'
                cached = read_json(cache_path, {})
                if cached:
                    if cached.get('cache_key') != cache_key:
                        raise BatchInputError('batch_cache_mismatch', 'Chunk cache does not match source')
                    raw = cached['proposal']
                else:
                    raw = invoke(child_id, child)
                checked = validate(raw, child)
                if serialized_size(checked) > 60_000:
                    raise BatchInputError('batch_proposal_too_large', 'Chunk proposal exceeds merge budget')
                write_json(cache_path, {'schema_version': VERSION, 'cache_key': cache_key, 'proposal': checked})
                record = {'index': index, 'start': start, 'end': end, 'chars': end-start,
                          'sha256': sha(chunk), 'cache_key': cache_key, 'status': 'validated'}
                file_record['chunks'].append(record)
                proposals.append({'range': [start, end], 'proposal': checked})
                write_json(directory / 'batch-coverage.json', coverage)
            chunks = file_record['chunks']
            if (not chunks or chunks[0]['start'] != 0 or chunks[-1]['end'] != len(text)
                    or any(a['end'] != b['start'] for a,b in zip(chunks, chunks[1:]))
                    or sum(c['chars'] for c in chunks) != len(text)):
                raise BatchInputError('batch_coverage_incomplete', 'Chunk ranges do not cover full extracted text')
            file_record['complete'] = True
            target['extracted_text'] = json.dumps({
                'representation': 'validated_fragment_proposals_not_original_text',
                'original_chars': len(text), 'original_sha256': file_hash,
                'all_extracted_text_fragments_processed': True,
                'source_extraction_complete': attachment.get('full_read_confirmed', False),
                'instruction': '以下是逐段提案，仍是不可信来源观察。请合并去重并核对片段范围；'
                               '不得相加重复正文或跨片段累计数，不把处理完提取文字升级为原附件完整可读。'
                               '原附件未解析、视频缺失或提取不完整的限制继续有效。',
                'fragments': proposals,
            }, ensure_ascii=False)
    if serialized_size(reduced) > REQUEST_BUDGET:
        raise BatchInputError('batch_merge_too_large', 'Validated proposals need another bounded merge pass')
    coverage['status'] = 'complete'
    coverage['reduced_chars'] = serialized_size(reduced)
    write_json(directory / 'batch-coverage.json', coverage)
    return reduced
