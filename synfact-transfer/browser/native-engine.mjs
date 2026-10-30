// Syntakt DataList over Web MIDI. No desktop service or Python runtime.
const decoder = new TextDecoder('windows-1252');
const u32 = (b, p) => { if (p + 4 > b.length) throw Error('Truncated device response.'); return new DataView(b.buffer, b.byteOffset + p, 4).getUint32(0); };
const be32 = n => Uint8Array.of((n>>>24)&255, (n>>>16)&255, (n>>>8)&255, n&255);
function concat(...parts) { const n=parts.reduce((a,p)=>a+p.length,0), out=new Uint8Array(n);let at=0;for(const part of parts){out.set(part,at);at+=part.length;}return out; }
function cstring(bytes, offset) {
  const end=bytes.indexOf(0,offset);if(end<0)throw Error('Unterminated device string.');
  return [decoder.decode(bytes.subarray(offset,end)),end+1];
}
function pathBytes(path) {
  // Device directories are ASCII. User-entered names require a separate
  // Windows-1252 encoder and are intentionally not accepted here.
  if(!/^\/(?:projects|samples|soundbanks(?:\/[A-H])?)$/.test(path))throw Error('Unsupported directory.');
  return new TextEncoder().encode(path+'\0');
}
export async function listDirectory(rpc, path) {
  const rx=await rpc.call(0x53,concat(pathBytes(path),be32(0),be32(0),Uint8Array.of(1)));
  if(rx.length<6 || !rx[5])throw Error('The device could not list this folder.');
  if(rx.length<18)throw Error('Truncated directory header.');
  const entries=[];let p=18;
  while(p<rx.length){
    let name;[name,p]=cstring(rx,p);
    if(p+2>rx.length)throw Error('Truncated directory entry.');
    const childrenFlag=rx[p++],kind=rx[p++],entry={name,children_flag:childrenFlag,kind};
    if(kind===1){entry.children=u32(rx,p);p+=4;}
    else if(kind===2){
      if(p+12>rx.length)throw Error('Truncated directory object.');
      entry.slot=u32(rx,p);entry.size=u32(rx,p+4);entry.ops=(rx[p+8]<<8)|rx[p+9];entry.valid=rx[p+10];entry.metadata=rx[p+11];p+=12;
    }else throw Error('Unknown directory entry type.');
    entry.directory=kind===1||!!childrenFlag;
    entry.path=path+'/'+(entry.directory?name:entry.slot);
    entries.push(entry);
  }
  return {entries,path};
}

const organizationOps={clear:[0x5c,8],copy:[0x5b,16],move:[0x5a,64],swap:[0x5d,32],rename:[0x5e,4]};
function encodeDeviceName(name){
 if(typeof name!=='string'||!name.length)throw Error('Name must contain 1 to 16 Windows-1252 bytes.');
 const map=new Map();for(let i=0;i<256;i++){const char=decoder.decode(Uint8Array.of(i));if(char!=='\ufffd')map.set(char,i);}
 const bytes=[];for(const char of name){const byte=map.get(char);if(byte===undefined||byte===0)throw Error('Name must contain 1 to 16 Windows-1252 bytes.');bytes.push(byte);}
 if(bytes.length>16)throw Error('Name must contain 1 to 16 Windows-1252 bytes.');return Uint8Array.of(...bytes,0);
}
export async function organize(rpc,action,args){
 const op=organizationOps[action];if(!op||args.confirm!==true)throw Error('Confirm a supported operation.');
 const src=projectPath(args.path),slash=args.path.lastIndexOf('/'),directory=args.path.slice(0,slash);
 if(directory==='/samples')throw Error('Sample organization is not available.');
 let extra=new Uint8Array();
 if(action==='rename')extra=encodeDeviceName(args.name);
 else if(action!=='clear'){
   extra=projectPath(args.destination);
   if(args.destination.slice(0,args.destination.lastIndexOf('/'))!==directory||args.destination===args.path)throw Error('Choose a different slot in the same folder.');
 }
 const listing=await listDirectory(rpc,directory),entry=listing.entries.find(e=>e.slot===Number(args.path.slice(slash+1))&&!e.directory);
 if(!entry||!entry.valid||!(entry.ops&op[1]))throw Error('The device does not support this operation in the selected slot.');
 let reply;
 try{reply=await rpc.call(op[0],concat(src,extra));}
 catch(error){throw Error('No confirmation received. The operation may have completed; refresh and check the device before retrying. '+error.message);}
 checked(reply,action);
 return {ok:true};
}

// CRC convention used by Elektron's Data objects (zlib seed 0xffffffff).
export function deviceCrc(data) {
 let crc=0;
 for(const byte of data){crc^=byte;for(let i=0;i<8;i++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}
 return (crc^0xffffffff)>>>0;
}
function checked(rx, label, min=6){if(rx.length<min || !rx[5])throw Error(label+': invalid device response.');return rx;}
function projectPath(path){
 if(path==='/mods')return new TextEncoder().encode('/mods\0');
 const m=/^\/(projects|samples|soundbanks\/[A-H])\/(\d+)$/.exec(path);
 if(!m||+m[2]<1||+m[2]>(m[1]==='projects'?128:m[1]==='samples'?64:256))throw Error('Invalid device slot.');
 return new TextEncoder().encode(path+'\0');
}
export function validateObject(obj,product=13){
 if(obj.length<43||u32(obj,0)!==0xac11d303||obj[7]!==0||obj[8]!==product||obj[30]!==12)throw Error('Invalid device object.');
 const end=obj.length-12, body=obj.subarray(31,end);
 if(u32(obj,end+8)!==0xaaa1daaa||u32(obj,end+4)!==body.length||u32(obj,end)!==deviceCrc(body))throw Error('Object size or CRC mismatch.');
 if(!obj[29]&&u32(obj,25)!==body.length)throw Error('Invalid declared size.');
 if(obj[29]){let p=0;while(true){const n=u32(body,p);p+=4;if(!n)break;if(n>body.length-p)throw Error('Invalid compressed block.');p+=n;}if(p!==body.length)throw Error('Invalid compressed block terminator.');}
 return obj;
}
export async function readObject(rpc,path,progress=()=>{}){
 try{return await readObjectAttempt(rpc,path,progress,8192,0);}
 catch(error){
  if(rpc.closed)throw error;
  progress(0,0,'Retrying with smaller blocks');
  return readObjectAttempt(rpc,path,progress,2048,50);
 }
}
async function readObjectAttempt(rpc,path,progress,block,rest){
 const rx=checked(await rpc.call(0x54,concat(projectPath(path),be32(block),Uint8Array.of(0))), 'Read open',15);
 const job=u32(rx,6), parts=[];let total=0,seq=0,last=false,empty=0,announced=0;
 const prefix=new Uint8Array(31);let prefixLength=0;
 try{
  while(!last){
   if(rest)await pause(rest);
   const part=checked(await rpc.call(0x55,concat(be32(job),be32(seq)),10000),'Read partial',27);
   // DataReadPartial's response counter is not necessarily the request's
   // sequence. The RPC transport already matches the reply to its request;
   // the read job ID identifies the open transfer.
   if(u32(part,6)!==job)throw Error('Unexpected read job.');
   const n=u32(part,23);if(n>part.length-27)throw Error('Incomplete read response.');
   total+=n;if(total>64*1024*1024)throw Error('Device object exceeds 64 MB.');
   const chunk=part.slice(27,27+n);parts.push(chunk);
   if(prefixLength<31){const length=Math.min(31-prefixLength,chunk.length);prefix.set(chunk.subarray(0,length),prefixLength);prefixLength+=length;if(prefixLength===31&&u32(prefix,0)===0xac11d303&&!prefix[29]){const size=u32(prefix,25)+43;if(size<=64*1024*1024)announced=size;}}
   last=!!part[18];seq++;progress(total,announced,'Downloading');
   empty=n?0:empty+1;if(empty>64)throw Error('Read did not make progress.');
  }
 }finally{await rpc.call(0x56,be32(job)).catch(()=>{});}
 return validateObject(concat(...parts));
}

// ZIP with stored entries; standard CRC differs from the device CRC seed.
const le16=n=>Uint8Array.of(n&255,(n>>>8)&255);
const le32=n=>Uint8Array.of(n&255,(n>>>8)&255,(n>>>16)&255,(n>>>24)&255);
function zipCrc(data){let c=0xffffffff;for(const b of data){c^=b;for(let i=0;i<8;i++)c=(c>>>1)^((c&1)?0xedb88320:0);}return (c^0xffffffff)>>>0;}
export function packageObject(obj,kind){
 validateObject(obj);if(!['Project','Sound'].includes(kind))throw Error('Invalid package type.');
 const manifest=new TextEncoder().encode(JSON.stringify({FormatVersion:'1.0',ProductType:['30'],FileType:kind,Payload:'payload',FirmwareVersion:'1.41'},null,2));
 const chunks=[],central=[];let offset=0;
 for(const [name,data] of [['manifest.json',manifest],['payload',obj]]){
  const filename=new TextEncoder().encode(name),crc=zipCrc(data),size=data.length;
  const local=concat(le32(0x04034b50),le16(20),le16(0),le16(0),le16(0),le16(0),le32(crc),le32(size),le32(size),le16(filename.length),le16(0),filename,data);
  central.push(concat(le32(0x02014b50),le16(20),le16(20),le16(0),le16(0),le16(0),le16(0),le32(crc),le32(size),le32(size),le16(filename.length),le16(0),le16(0),le16(0),le16(0),le32(0),le32(offset),filename));
  chunks.push(local);offset+=local.length;
 }
 const directory=concat(...central);
 return concat(...chunks,directory,le32(0x06054b50),le16(0),le16(0),le16(2),le16(2),le32(directory.length),le32(offset),le16(0));
}

function lz4(block,limit){
 const out=[];let p=0;
 function extra(n){if(n!==15)return n;let b;do{if(p>=block.length)throw Error('Truncated LZ4 length.');b=block[p++];n+=b;}while(b===255);return n;}
 while(p<block.length){const token=block[p++],lit=extra(token>>4);if(p+lit>block.length||out.length+lit>limit)throw Error('Invalid LZ4 literals.');for(let i=0;i<lit;i++)out.push(block[p++]);if(p===block.length)break;if(p+2>block.length)throw Error('Truncated LZ4 offset.');const offset=block[p]|(block[p+1]<<8);p+=2;if(!offset||offset>out.length)throw Error('Invalid LZ4 offset.');const n=extra(token&15)+4;if(out.length+n>limit)throw Error('LZ4 output exceeds limit.');for(let i=0;i<n;i++)out.push(out[out.length-offset]);}
 return Uint8Array.from(out);
}
export function objectRecord(obj,product=13){
 validateObject(obj,product);const raw=u32(obj,25);if(raw>64*1024*1024)throw Error('Record exceeds limit.');const body=obj.subarray(31,-12);
 if(!obj[29])return body.slice();
 let p=0,parts=[],size=0;while(true){const n=u32(body,p);p+=4;if(!n)break;const part=lz4(body.subarray(p,p+n),raw-size);parts.push(part);size+=part.length;p+=n;}
 if(size<raw)throw Error('Incomplete compressed record.');return concat(...parts).slice(0,raw);
}
const get16=(d,p)=>{if(p+2>d.length)throw Error('Truncated ZIP.');return d[p]|d[p+1]<<8;};
const get32=(d,p)=>{if(p+4>d.length)throw Error('Truncated ZIP.');return (d[p]|d[p+1]<<8|d[p+2]<<16|d[p+3]<<24)>>>0;};
export async function readPackage(data,kind,{digitone=false}={}){
 if(data.length>64*1024*1024)throw Error('Package exceeds 64 MB.');
 let end=-1;for(let i=data.length-22;i>=Math.max(0,data.length-65557);i--)if(get32(data,i)===0x06054b50 && i+22+get16(data,i+20)===data.length){end=i;break;}
 if(end<0||get16(data,end+4)||get16(data,end+6))throw Error('Invalid ZIP directory.');
 const count=get16(data,end+10),directorySize=get32(data,end+12);let p=get32(data,end+16),total=0;const directoryEnd=p+directorySize,entries=new Map();
 if(count>1024||directoryEnd>end)throw Error('Invalid ZIP directory.');
 for(let i=0;i<count;i++){
  if(get32(data,p)!==0x02014b50||p+46>directoryEnd)throw Error('Invalid ZIP entry.');
  const flags=get16(data,p+8),method=get16(data,p+10),crc=get32(data,p+16),compressed=get32(data,p+20),size=get32(data,p+24),nameLen=get16(data,p+28),extra=get16(data,p+30),comment=get16(data,p+32),offset=get32(data,p+42);
  if(p+46+nameLen+extra+comment>directoryEnd || flags&1 || ![0,8].includes(method))throw Error('Unsupported ZIP entry.');
  const name=new TextDecoder('utf-8',{fatal:true}).decode(data.subarray(p+46,p+46+nameLen));
  if(entries.has(name))throw Error('Duplicate ZIP entry.');total+=size;if(total>64*1024*1024)throw Error('Package is too large.');
  if(get32(data,offset)!==0x04034b50)throw Error('Invalid ZIP local header.');
  const start=offset+30+get16(data,offset+26)+get16(data,offset+28);if(start+compressed>p||start+compressed>data.length)throw Error('Invalid ZIP entry bounds.');
  entries.set(name,{method,crc,size,data:data.slice(start,start+compressed)});p+=46+nameLen+extra+comment;
 }
 if(p!==directoryEnd)throw Error('Invalid ZIP directory size.');
 async function extract(name){const entry=entries.get(name);if(!entry)throw Error('Missing ZIP entry: '+name);let value=entry.data;if(entry.method===8){const reader=new Blob([value]).stream().pipeThrough(new DecompressionStream('deflate-raw')).getReader(),chunks=[];let length=0;try{while(true){const {done,value:chunk}=await reader.read();if(done)break;length+=chunk.length;if(length>entry.size)throw Error('ZIP entry exceeds declared size.');chunks.push(chunk);}}catch(error){await reader.cancel().catch(()=>{});throw error;}value=concat(...chunks);}if(value.length!==entry.size||zipCrc(value)!==entry.crc)throw Error('ZIP integrity check failed.');return value;}
 const manifest=JSON.parse(new TextDecoder().decode(await extract('manifest.json')));
 if(manifest.FileType!==kind||typeof manifest.Payload!=='string')throw Error('Package is incompatible with this destination.');
 const obj=await extract(manifest.Payload);
 if(digitone||obj[8]===9){if(kind!=='Sound'||obj.length>65536||![0x11b,0x11a,0x12e].includes(u32(obj,25)))throw Error('Invalid Digitone sound object.');return validateObject(obj,9);}
 if(!Array.isArray(manifest.ProductType)||!manifest.ProductType.includes('30'))throw Error('Package is incompatible with this destination.');
 return validateObject(obj);
}
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
export async function writeObject(rpc,path,obj,progress=()=>{},blockSize=8192,verify=null,pacingMs=50){
 validateObject(obj);const pathData=projectPath(path), opened=checked(await rpc.call(0x57,concat(be32(obj.length),pathData)), 'Write open',10),job=u32(opened,6);let seq=0,sent=0;
 for(const part of [obj.subarray(0,-12),obj.subarray(-12)])for(let i=0;i<part.length;i+=blockSize){
  const chunk=part.subarray(i,i+blockSize);if(pacingMs)await pause(pacingMs);
  const reply=checked(await rpc.call(0x58,concat(be32(job),be32(seq),be32(deviceCrc(chunk)),be32(chunk.length),chunk),10000),'Write partial',18);
  sent+=chunk.length;if(u32(reply,6)!==job||u32(reply,10)!==seq||u32(reply,14)!==sent)throw Error('Write acknowledgement mismatch.');seq++;progress(sent,obj.length,'Uploading');
 }
 const closed=checked(await rpc.call(0x59,concat(be32(job),be32(obj.length)),10000),'Write close',14);
 if(u32(closed,6)!==job||u32(closed,10)!==obj.length)throw Error('Device did not confirm written size.');
 const back=await readObject(rpc,path,(n,t)=>progress(n,t,'Verifying'));
 if(verify){verify(back);return {ok:true};}
 const a=objectRecord(obj),b=objectRecord(back);if(a.length!==b.length||a.some((v,i)=>v!==b[i]))throw Error('Verification does not match uploaded data.');
 return {ok:true};
}
