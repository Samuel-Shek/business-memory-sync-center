"""Version-bound image verification; no model calls or business writes."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json, os, subprocess, sys
from pathlib import Path
import video_hold, media_sources
POLICY = 'bound-image-intake-v1'

class IntakeError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def exact_image_reference(h, info, message):
    ref = json.loads(h['object_ref'])
    resource = info['resource']
    return (h['kind'] == 'unknown_media' and message.get('openMessageId') == ref.get('message_id')
            and message.get('openConversationId') == info['args'].get('open-conversation-id')
            and resource.get('type') == 'mediaId' and bool(resource.get('resourceId'))
            and '[图片消息](mediaId=' + resource['resourceId'] + ')' in str(message.get('content', '')))

def require_current(h, info, config):
    current = video_hold.by_id(h['hold_id'])
    if not current or current['version'] != h['version'] or current['object_ref'] != h['object_ref']:
        raise IntakeError('media_source_changed')
    actual = media_sources.resolve(current, config)
    if not actual.get('ready') or actual.get('resource') != info['resource'] or actual.get('args') != info['args']:
        raise IntakeError('media_source_changed')

def decode_local(source, directory):
    # Run only in the already installed image runtime. All frames are decoded.
    import image_frame_coverage as frames
    import media_processor as processor
    from PIL import Image
    import warnings
    warnings.simplefilter('error', Image.DecompressionBombWarning)
    source, directory = Path(source), Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if source.is_symlink() or directory.is_symlink():raise IntakeError('media_cache_path')
    before = processor.sha(source)
    with Image.open(source) as image:
        if image.format=='GIF':
            with source.open('rb') as stream:
                stream.seek(-1,2)
                if stream.read(1)!=b';':raise IntakeError('image_decode_failed')
        image.verify()
    fmt, files, coverage = frames.decode(source, directory)
    frames.verify(source, directory, coverage)
    if processor.sha(source) != before:raise IntakeError('media_source_changed')
    return {'policy': POLICY, 'source_sha256': before, 'format': fmt, 'frame_coverage': coverage,
            'files': {p.name: processor.sha(p) for p in files}}

def decoded_image(config, source, directory):
    import media_processor as processor
    python = config.get('email_sync', {}).get('phase_python') or sys.executable
    try:
        r = subprocess.run([python, '-B', str(Path(__file__).resolve()), '--decode', str(source), str(directory)],
                           capture_output=True, text=True, timeout=60)
        value = json.loads(r.stdout)
    except subprocess.TimeoutExpired:raise IntakeError('image_decode_timeout')
    except (OSError, ValueError):raise IntakeError('image_decoder_unavailable')
    if r.returncode or value.get('policy') != POLICY:
        raise IntakeError(value.get('error_code', 'image_decode_failed'))
    if value.get('source_sha256') != processor.sha(source):raise IntakeError('media_source_changed')
    for name, digest in value['files'].items():
        p = Path(directory) / name
        if Path(name).name != name or p.is_symlink() or processor.sha(p) != digest:raise IntakeError('image_frame_hash')
    processor.save(Path(directory) / 'coverage.json', value)
    return value

def acquire_image(h, info, config):
    import media_processor as processor
    require_current(h, info, config)
    work = processor.RUNS / h['hold_id'] / video_hold.digest(h['version'])[:16]
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    source, binding = work/'source/original.media', work/'source.json'
    if any(p.is_symlink() for p in (work, work.parent, source.parent, source, binding)):
        raise IntakeError('media_cache_path')
    if source.exists() or binding.exists():
        if not source.is_file() or not binding.is_file():raise IntakeError('media_cache_incomplete')
        meta = json.loads(binding.read_text())
        if meta.get('version') != h['version'] or meta.get('hold_id') != h['hold_id'] or processor.sha(source) != meta.get('source_sha256'):
            raise IntakeError('media_cache_mismatch')
    else:
        intent=work/'acquisition.json'
        if intent.exists() and json.loads(intent.read_text()).get('state')=='inflight':
            raise IntakeError('media_acquisition_uncertain')
        processor.save(intent,{'policy':POLICY,'state':'inflight','version':h['version'],
                               'resource_sha256':video_hold.digest(info['resource'])})
        try:source, meta = media_sources.acquire(h, config, work/'source')
        except media_sources.SourceError as exc:
            if exc.code in {'media_download_denied','media_download_failed'}:
                processor.save(intent,{'policy':POLICY,'state':'failed','version':h['version'],'reason_code':exc.code})
            raise
        # Persist acquisition before decoding so a decode failure cannot re-download.
        processor.save(binding, meta)
        processor.save(intent,{'policy':POLICY,'state':'acquired','version':h['version'],'source_sha256':meta['source_sha256']})
    require_current(h, info, config)
    try:
        proof = decoded_image(config, source, work/'image-proof')
    except IntakeError as exc:
        # A corrupt/oversized image is not relabelled as video by ffprobe's image stream.
        if exc.code == 'image_format_unrecognized':
            if processor.automatic_kind(config, source) == 'video':
                return 'video', {'method':'bound_original_format_probe', 'source_sha256':meta['source_sha256'],
                                 'original_resource_sha256':video_hold.digest(info['resource']), 'reason':'实际原件为视频，停止内容处理。'}
        raise
    require_current(h, info, config)
    return 'image', {'method':'fresh_exact_image_reference_and_decoded_original',
                     'original_resource_sha256':video_hold.digest(info['resource']),
                     'source_sha256':meta['source_sha256'], 'format':proof['format'],
                     'frames':proof['frame_coverage']['frame_count'], 'evidence_ref':str(work/'image-proof/coverage.json')}

if __name__ == '__main__':
    os.umask(0o077)
    try:
        if len(sys.argv)!=4 or sys.argv[1]!='--decode':raise IntakeError('image_decoder_arguments')
        print(json.dumps(decode_local(sys.argv[2], sys.argv[3])))
    except Exception as exc:
        from PIL import UnidentifiedImageError
        code = exc.code if isinstance(exc, IntakeError) else 'image_format_unrecognized' if isinstance(exc, UnidentifiedImageError) else 'image_frame_budget' if str(exc)=='image_frame_budget' else 'image_decode_failed'
        print(json.dumps({'error_code':code}));raise SystemExit(2)
