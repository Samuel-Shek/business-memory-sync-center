"""Bounded local frame decoding. No networking, credentials or active content."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

from pathlib import Path
import hashlib
MAX_FRAMES = 32
MAX_TOTAL_PIXELS = 64000000

def pixel_hash(frame):
    return hashlib.sha256(str(frame.size).encode() + frame.tobytes()).hexdigest()

def decode(source, directory):
    from PIL import Image, ImageOps
    directory = Path(directory)
    with Image.open(source) as im:
        fmt = im.format
        if fmt not in {"PNG", "JPEG", "AVIF", "WEBP", "GIF", "TIFF", "BMP", "HEIF"}:
            raise ValueError("image_format")
        count = getattr(im, "n_frames", 1)
        if not 1 <= count <= MAX_FRAMES or im.width * im.height * count > MAX_TOTAL_PIXELS:
            raise ValueError("image_frame_budget")
        frames, files, unique = [], [], {}
        total_pixels = 0
        for index in range(count):
            im.seek(index)
            total_pixels += im.width * im.height
            if total_pixels > MAX_TOTAL_PIXELS:raise ValueError('image_frame_budget')
            frame = ImageOps.exif_transpose(im.copy()).convert("RGB")
            key = pixel_hash(frame)
            name = unique.get(key)
            if name is None:
                name = "frame-%03d.png" % index
                path = directory / name
                frame.save(path, format="PNG"); path.chmod(0o600)
                unique[key] = name; files.append(path)
            frames.append({"index": index, "file": name, "pixels_sha256": key})
    return fmt, files, {"frame_count": count, "frames": frames, "complete": True}

def verify(source,directory,coverage):
    from PIL import Image,ImageOps
    with Image.open(source) as image:
        count=getattr(image,'n_frames',1)
        frames=coverage.get('frames',[])
        if coverage.get('complete') is not True or coverage.get('frame_count')!=count or not 1<=count<=MAX_FRAMES or image.width*image.height*count>MAX_TOTAL_PIXELS or [f.get('index') for f in frames]!=list(range(count)):
            raise ValueError('frame_coverage')
        total_pixels=0
        for item in frames:
            image.seek(item['index'])
            total_pixels+=image.width*image.height
            if total_pixels>MAX_TOTAL_PIXELS:raise ValueError('image_frame_budget')
            actual=pixel_hash(ImageOps.exif_transpose(image.copy()).convert('RGB'))
            name=item['file']
            if Path(name).name!=name:raise ValueError('frame_path')
            with Image.open(Path(directory)/name) as rendered:
                if actual!=pixel_hash(rendered.convert('RGB')) or actual!=item['pixels_sha256']:raise ValueError('frame_pixels')
    return True

if __name__=='__main__':
    import sys,json
    try:
        verify(sys.argv[1],sys.argv[2],json.load(sys.stdin));print('{"verified":true}')
    except Exception:
        print('{"verified":false}');raise SystemExit(2)
