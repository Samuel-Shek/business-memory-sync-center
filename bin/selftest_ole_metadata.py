
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import asyncio,io,struct,tempfile,unittest,sys
from pathlib import Path
from unittest.mock import Mock,patch
import mail_attachment_reader as r
class OLE(unittest.TestCase):
 def header(self,name,payload):
  raw=struct.pack('<H',2)+name.encode()+b'\0C:\\fixture\0'+b'\0'*8+b'fixture\0'+struct.pack('<I',len(payload))+payload
  return struct.pack('<I',len(raw))+raw
 def test_video_header_never_reads_member_payload(self):
  header=self.header('fixture.mp4',b'\0\0\0\0ftyp')
  with tempfile.TemporaryDirectory() as t:
   reader=r.Reader(t);archive=Mock();archive.open.return_value=io.BytesIO(bytes.fromhex('d0cf11e0a1b11ae1'))
   ole=Mock();ole.__enter__=Mock(return_value=ole);ole.__exit__=Mock();ole.openstream.return_value=io.BytesIO(header)
   with patch.object(r,'ole_native_prefix',return_value=header):
    node=asyncio.run(reader.member(archive,Mock(filename='xl/embeddings/oleObject.bin',file_size=1000),'fixture.xlsx',1))
   archive.read.assert_not_called();self.assertEqual(node['status'],'deferred_video');self.assertFalse(node['content_read'])
 def test_pdf_requires_matching_signature(self):
  for signature,allowed in ((b'%PDF-1.7',True),(b'not-pdf',False)):
   header=self.header('fixture.pdf',signature)
   with tempfile.TemporaryDirectory() as t:
    reader=r.Reader(t);archive=Mock();archive.open.return_value=io.BytesIO(bytes.fromhex('d0cf11e0a1b11ae1'));archive.read.return_value=b'container'
    ole=Mock();ole.__enter__=Mock(return_value=ole);ole.__exit__=Mock();ole.openstream.return_value=io.BytesIO(header)
    async def read(*args,**kwargs):return {'verified_ole':kwargs.get('verified_ole')}
    with patch.object(r,'ole_native_prefix',return_value=header),patch.object(reader,'read',side_effect=read):
     node=asyncio.run(reader.member(archive,Mock(filename='xl/embeddings/oleObject.bin',file_size=1000),'fixture.xlsx',1))
    self.assertEqual(archive.read.called,allowed)
 def test_cfb_prefix_does_not_read_native_tail(self):
  header=bytearray(512);header[:8]=bytes.fromhex('d0cf11e0a1b11ae1');header[28:30]=b'\xfe\xff';header[30:32]=struct.pack('<H',9);header[44:48]=struct.pack('<I',1);header[48:52]=struct.pack('<I',1);header[76:80]=struct.pack('<I',0)
  fat=[0xffffffff]*128;fat[0]=0xfffffffd;fat[1]=0xfffffffe
  for i in range(2,10):fat[i]=i+1
  fat[10]=0xfffffffe
  directory=bytearray(512);name='\x01Ole10Native'.encode('utf-16le')+b'\0\0';directory[:len(name)]=name;directory[64:66]=struct.pack('<H',len(name));directory[66]=2;directory[116:120]=struct.pack('<I',2);directory[120:128]=struct.pack('<Q',4096)
  native=self.header('fixture.mp4',b'video-not-to-be-read'*250)
  source=bytes(header)+struct.pack('<128I',*fat)+bytes(directory)+native.ljust(4096,b'\0')
  class Restricted(io.BytesIO):
   def read(self,n=-1):
    if self.tell()>=2048:raise AssertionError('native tail read')
    return super().read(n)
  value=r.ole_native_prefix(Restricted(source),len(source));self.assertEqual(r.ole_header(value)[0],'fixture.mp4')

 def test_incomplete_header_fails_closed(self):
  with self.assertRaises(ValueError):r.ole_header(b'\0'*6)
if __name__=='__main__':unittest.main()
