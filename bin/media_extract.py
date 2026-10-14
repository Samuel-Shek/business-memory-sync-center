"""Read exactly one named embedded member; never execute Office or embedded objects."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib,io,struct,zipfile
LIMIT=512*1024*1024

def sha(data):return hashlib.sha256(data).hexdigest()
def native(data):
    cursor=4
    def u16():
        nonlocal cursor
        n=struct.unpack_from('<H',data,cursor)[0];cursor+=2;return n
    def u32():
        nonlocal cursor
        n=struct.unpack_from('<I',data,cursor)[0];cursor+=4;return n
    def string():
        nonlocal cursor
        end=data.index(b'\0',cursor);v=data[cursor:end];cursor=end+1;return v.decode('cp1252',errors='replace')
    u16();name=string();string();u32();u32();string();count=u32()
    if count>LIMIT or count>len(data)-cursor:raise ValueError('embedded_size_invalid')
    return name,data[cursor:cursor+count]

def member(data,chain):
    parts=chain.split(' / ')[1:]
    if not parts or len(parts)>12:raise ValueError('embedded_member_invalid')
    for name in parts:
        if len(data)>LIMIT:raise ValueError('media_size_limit')
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                i=z.getinfo(name)
                if i.file_size>LIMIT or i.flag_bits&1 or i.filename.startswith('/') or '..' in i.filename.split('/'):
                    raise ValueError('embedded_member_unsafe')
                data=z.read(i)
        elif data.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
            import olefile
            with olefile.OleFileIO(io.BytesIO(data)) as ole:
                found=[]
                for s in ole.listdir():
                    if s[-1]=='\x01Ole10Native':
                        n,payload=native(ole.openstream(s).read())
                        if n.replace('\\','/').split('/')[-1]==name:found.append(payload)
                if len(found)!=1:raise ValueError('embedded_member_not_unique')
                data=found[0]
        else:raise ValueError('embedded_container_unsupported')
    return data
