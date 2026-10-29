# SPDX-License-Identifier: MIT
"""Portable HZFS v1/v2 metadata codec. No IO, MIDI or device side effects.

V2 retains every 64-byte sample record and appends n u32 folder IDs then
f 48-byte folder records (id,parent,32-byte UTF-8 name,8 reserved zero bytes).
"""
import struct
import zlib

WINDOW=0x80000
ENTRIES_OFF=0x140
ENTRY=64
FOLDER_BYTES=48
MAX_FOLDERS=1024
MAX_DEPTH=16
MAX_ENTRIES=(WINDOW-ENTRIES_OFF)//ENTRY
DATA_FIRST_K=0x602

def crc(raw):return zlib.crc32(raw,0xffffffff)&0xffffffff

def logical_length(version,count,folders=0):
    if version not in (1,2) or not 0<=count<=MAX_ENTRIES or not 0<=folders<=MAX_FOLDERS:
        raise ValueError('Unsupported or oversized sample catalogue')
    size=ENTRIES_OFF+count*64+(count*4+folders*FOLDER_BYTES if version==2 else 0)
    if size>WINDOW or (version==1 and folders):raise ValueError('Sample catalogue full')
    return size

def name_ok(name):
    return isinstance(name,str) and bool(name) and name not in ('.','..') and not any(c in name for c in '/\\\0') and not any(ord(c)<32 for c in name) and len(name.encode('utf-8'))<=32

class Index:
    def __init__(self,seq=0,windows=0,next_id=1,entries=None,slot_map=None,*,version=1,folders=None,next_folder=1):
        self.seq,self.windows,self.next_id=seq,windows,next_id
        self.entries=[] if entries is None else entries
        self.slot_map=[0]*64 if slot_map is None else slot_map
        self.version=version;self.folders=[] if folders is None else folders;self.next_folder=next_folder
    def pack(self):
        n,f=len(self.entries),len(self.folders)
        raw=bytearray(logical_length(self.version,n,f))
        struct.pack_into('>9I',raw,4,self.version,self.seq,n,WINDOW,DATA_FIRST_K,self.windows,self.next_id,0,len(raw))
        struct.pack_into('>64I',raw,64,*self.slot_map)
        if self.version==2:struct.pack_into('>2I',raw,40,f,self.next_folder)
        for i,e in enumerate(self.entries):
            name=e['name'].encode('utf-8')
            if not 1<=len(name)<=32 or b'\0' in name:raise ValueError('Sample name must use 1..32 UTF-8 bytes')
            struct.pack_into('>8I32s',raw,ENTRIES_OFF+64*i,*[e[k] for k in ('id','first','blocks','bytes','frames','crc','created','flags')],name)
            if self.version==2:struct.pack_into('>I',raw,ENTRIES_OFF+64*n+4*i,e.get('folder',0))
        if self.version==2:
            for i,d in enumerate(self.folders):
                if not name_ok(d['name']):raise ValueError('Invalid folder name')
                struct.pack_into('>2I32s',raw,ENTRIES_OFF+68*n+48*i,d['id'],d['parent'],d['name'].encode('utf-8'))
        raw[:4]=b'HZFS';struct.pack_into('>I',raw,32,crc(raw))
        return bytes(raw)
    @classmethod
    def unpack(cls,raw):
        try:
            if len(raw)<ENTRIES_OFF or raw[:4]!=b'HZFS':return None
            version,seq,n,block,first,windows,next_id,check,size=struct.unpack_from('>9I',raw,4)
            f,next_folder=struct.unpack_from('>2I',raw,40) if version==2 else (0,1)
            if block!=WINDOW or first!=DATA_FIRST_K or size!=logical_length(version,n,f) or size>len(raw):return None
            if crc(raw[:32]+bytes(4)+raw[36:size])!=check:return None
            entries=[];folders=[]
            for i in range(n):
                words=struct.unpack_from('>8I32s',raw,ENTRIES_OFF+64*i)
                e=dict(zip(('id','first','blocks','bytes','frames','crc','created','flags'),words[:8]))
                e['name']=words[8].split(b'\0')[0].decode('utf-8')
                if version==2:e['folder']=struct.unpack_from('>I',raw,ENTRIES_OFF+64*n+4*i)[0]
                entries.append(e)
            for i in range(f):
                at=ENTRIES_OFF+68*n+48*i
                ident,parent,name=struct.unpack_from('>2I32s',raw,at)
                if any(raw[at+40:at+48]):return None
                folders.append(dict(id=ident,parent=parent,name=name.split(b'\0')[0].decode('utf-8')))
            return cls(seq,windows,next_id,entries,list(struct.unpack_from('>64I',raw,64)),version=version,folders=folders,next_folder=next_folder)
        except (ValueError,UnicodeError,struct.error,OverflowError):return None
    def used(self):return [(e['first'],e['first']+e['blocks']) for e in self.entries]
    def find(self,key):
        key=str(key)
        # Numeric IDs are unambiguous even with same leaf names in folders.
        matches=[e for e in self.entries if str(e['id'])==key] if key.isdigit() else [e for e in self.entries if self.sample_path(e)==key.strip('/') or e['name']==key]
        if len(matches)!=1:raise KeyError('Sample not found or name ambiguous; use its ID or full path')
        return matches[0]
    def folder(self,ident):
        if not ident:return dict(id=0,parent=0,name='')
        for d in self.folders:
            if d['id']==ident:return d
        raise ValueError('Folder does not exist')
    def path(self,ident):
        parts=[];seen=set()
        while ident:
            if ident in seen or len(parts)>=MAX_DEPTH:raise ValueError('Folder cycle or depth limit')
            seen.add(ident);d=self.folder(ident);parts.append(d['name']);ident=d['parent']
        return '/'.join(reversed(parts))
    def sample_path(self,e):return '/'.join(filter(None,(self.path(e.get('folder',0)),e['name'])))
    def metadata_problems(self):
        errors=[]
        try:logical_length(self.version,len(self.entries),len(self.folders))
        except ValueError as exc:errors.append(str(exc))
        if self.version==1:
            if self.folders or any(e.get('folder',0) for e in self.entries):errors.append('Folders require catalogue v2')
            return errors
        ids=[d['id'] for d in self.folders]
        if len(ids)!=len(set(ids)) or any(not 0<i<min(self.next_folder,32767) for i in ids) or not 0<self.next_folder<=0xffffffff:errors.append('Invalid folder IDs')
        names=set()
        for d in self.folders:
            pair=(d['parent'],d['name'])
            if pair in names or not name_ok(d['name']):errors.append('Invalid or duplicate folder name')
            names.add(pair)
            try:self.path(d['id'])
            except ValueError as exc:errors.append(str(exc))
        for e in self.entries:
            try:self.folder(e.get('folder',0))
            except ValueError as exc:errors.append(str(exc))
        return errors
    def mkdir(self,name,parent=0):
        if self.version!=2:raise ValueError('Enable folders first (requires new firmware)')
        self.folder(parent)
        if not name_ok(name) or any(d['parent']==parent and d['name']==name for d in self.folders):raise ValueError('Invalid or duplicate folder name')
        if len(self.folders)>=MAX_FOLDERS or self.next_folder>=32767:raise ValueError('Folder limit reached')
        if len(self.path(parent).split('/'))>=MAX_DEPTH and parent:raise ValueError('Folder depth limit')
        logical_length(2,len(self.entries),len(self.folders)+1)
        ident=self.next_folder;self.next_folder+=1;self.folders.append(dict(id=ident,parent=parent,name=name));return ident
    def change_folder(self,ident,*,name=None,parent=None):
        if not ident:raise ValueError('Cannot change root')
        d=self.folder(ident);saved=dict(d)
        if name is not None:d['name']=name
        if parent is not None:d['parent']=parent
        errors=self.metadata_problems()
        if errors:d.update(saved);raise ValueError('; '.join(errors))
    def rmdir(self,ident):
        self.folder(ident)
        if not ident or any(d['parent']==ident for d in self.folders) or any(e.get('folder',0)==ident for e in self.entries):raise ValueError('Only empty folders can be removed')
        self.folders=[d for d in self.folders if d['id']!=ident]
    def move_samples(self,ids,folder):
        if self.version!=2:raise ValueError('Enable folders first')
        self.folder(folder);chosen=[self.find(str(i)) for i in ids]
        names=[e['name'] for e in chosen]
        if len(names)!=len(set(names)) or any(e not in chosen and e.get('folder',0)==folder and e['name'] in names for e in self.entries):raise ValueError('Duplicate sample name in destination')
        for e in chosen:e['folder']=folder


def enable_folders(store,backup,*,firmware_confirmed=False):
    """Explicit, resumable v1 -> v2 metadata-only migration. Never automatic.

    Save both validated metadata copies before the first write. Publish and
    verify v2 in BOTH windows so old writers cannot silently use a stale v1.
    A power loss can leave a mixed pair; re-running completes it. No PCM IO.
    """
    import hashlib,json,copy,os
    from pathlib import Path
    if not firmware_confirmed:raise ValueError('Install folder-capable core 0.13+ before enabling folders')
    current,copies=store.current()
    if current.version==2 and all(c and c.version==2 for c in copies):return current
    target=Path(backup).expanduser()
    if not target.is_absolute():raise ValueError('Backup folder must be an absolute path')
    target.mkdir(parents=True,exist_ok=False)
    records=[]
    for i,c in enumerate(copies):
        if c:
            raw=c.pack();name='catalogue-'+str(i)+'.hzfs'
            with (target/name).open('xb') as output:
                output.write(raw);output.flush();os.fsync(output.fileno())
            if (target/name).read_bytes()!=raw:raise IOError('Backup verification failed')
            records.append(dict(file=name,sha256=hashlib.sha256(raw).hexdigest()))
    (target/'manifest.json').write_text(json.dumps(dict(kind='HZFS metadata before folders',files=records),indent=2)+'\n')
    new=copy.deepcopy(current);new.version=2
    if new.metadata_problems():raise ValueError('Catalogue cannot migrate safely')
    new.pack() # Capacity/encoding check before the first write.
    for _ in range(2):
        store.commit(new,copies)
        new,copies=store.current()
    if not all(c and c.version==2 for c in copies):raise IOError('Incomplete folder migration; run it again')
    return new
