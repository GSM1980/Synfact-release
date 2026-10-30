// Browser port of mods_file.py, core.machine_info and firmware transfer.
import {deviceCrc,writeObject,validateObject} from './native-engine.mjs';
const be=(a,p)=>{if(p<0||p+4>a.length)throw Error('Truncated mods file.');return new DataView(a.buffer,a.byteOffset+p,4).getUint32(0);};
const u32=n=>Uint8Array.of(n>>>24,(n>>>16)&255,(n>>>8)&255,n&255);
function concat(...parts){const a=new Uint8Array(parts.reduce((n,p)=>n+p.length,0));let p=0;for(const part of parts){a.set(part,p);p+=part.length;}return a;}
function adler(data){let a=1,b=0;for(const x of data){a=(a+x)%65521;b=(b+a)%65521;}return (b*65536+a)>>>0;}
function sum(data,start,end){let n=0;for(let p=start;p<end;p+=4)n=(n+be(data,p))>>>0;return n;}
function fail(message){throw Error(message);}
export function inspectMods(data){
 if(data.length>16*1024*1024)fail('The mods file exceeds 16 MiB.');
 const magic=new TextDecoder('latin1').decode(data.subarray(0,4));
 if(magic==='SAFE'){
  if(data.length<48)fail('Incomplete SAFE mods header.');
  const version=be(data,4),length=be(data,12);if(length!==data.length||be(data,20)!==48||adler(data.subarray(20))!==be(data,16))fail('Invalid SAFE header or checksum.');
  if(version===2){
   if(data.length<80||data.length%16||be(data,8)!==0x454c0001)fail('Invalid Elekloader SAFE header or ABI.');
   const features=be(data,24),ui=be(data,28),modules=be(data,32),nmods=be(data,36),desc=be(data,40),count=be(data,44),image=be(data,52),nrel=be(data,56),hooks=be(data,60),init=be(data,64),checksum=be(data,68),audioAt=48+be(data,72),audioSize=be(data,76),start=80+4*nrel,end=start+image;
   if((features&~7)||count>16||nmods>16||!(count||nmods)||Boolean(features&4)!==Boolean(nmods)||(!nmods&&modules)||(!(features&2)&&ui)||(!count&&(features&2)))fail('Invalid Elekloader bank directories.');
   if(be(data,48)!==0x534d5a34||hooks||init!==0xffffffff||!image||image%4||end>Math.min(data.length,0x200000)||audioAt%16||audioAt<end||audioSize<400||audioSize>723120||audioAt+Math.ceil(audioSize/2048)*2048!==data.length)fail('Invalid Elekloader MAIN/AUDIO bounds.');
   if(desc%4||desc+40*count>image||((features&2)&&(ui%4||ui+32*count>image))||(nmods&&(modules%4||modules+16*nmods>image)))fail('Invalid Elekloader directory bounds.');
   if(sum(data,80,end)!==checksum)fail('Mods MAIN checksum mismatch.');
   let prev=0;for(let p=80;p<start;p+=4){const at=be(data,p);if(at%2||at<prev||at+4>image||be(data,start+at)>=image)fail('Invalid Elekloader relocation.');prev=at+4;}
   if(data.subarray(end,audioAt).some(Boolean)||data.subarray(audioAt+audioSize).some(Boolean))fail('Invalid alignment padding.');
   const audio=data.subarray(audioAt,audioAt+audioSize);if(be(audio,0)!==0x534d4133||be(audio,4)!==count||be(audio,8)!==audioSize||400+16*count>audioSize||adler(audio.subarray(16))!==be(audio,12))fail('Invalid AUDIO checksum or header.');
   const machines=[],audioTypes=[];for(let i=0;i<count;i++){machines.push(be(data,start+desc+40*i));audioTypes.push(be(audio,16+16*i));}
   if(new Set(machines).size!==count||machines.some(n=>n<64||n>=80)||machines.slice().sort((a,b)=>a-b).some((n,i)=>n!==audioTypes.slice().sort((a,b)=>a-b)[i]))fail('Invalid machine types.');
   return {format:'SAFE',sites:0,loader:'elekloader',image_bytes:image};
  }
  if(version!==1||be(data,44))fail('Unsupported SAFE version.');
  const patches=be(data,24),count=be(data,28),relocs=be(data,32),nrel=be(data,36),slots=be(data,40);
  if(count<1||count>256||patches<80||patches%4||patches+count*16>relocs||relocs%4||relocs+nrel*4!==data.length)fail('Invalid SAFE tables.');
  for(let i=0;i<count;i++){const p=patches+i*16,address=be(data,p),size=be(data,p+4),before=be(data,p+8),after=be(data,p+12);if(!size||address%2||[before,after].some(off=>off<patches+count*16||off+size>relocs))fail('Invalid SAFE patch bounds.');}
  for(let i=0;i<nrel;i++){const off=be(data,relocs+4*i);if(off%2||off<patches+count*16||off+4>relocs)fail('Invalid SAFE relocation.');}
  if(new TextDecoder('latin1').decode(data.subarray(48,52))==='SAFE')fail('Nested mods envelopes are not supported.');
  const inner=inspectMods(data.subarray(48,patches));if(slots>=inner.image_bytes)fail('Invalid mods export table offset.');return {...inner,format:'SAFE'};
 }
 if(data.length<32||!(magic==='SMZ4'||/^S[0-9]{1,2}Z[0-9]?$/.test(magic)))fail('A valid mods file is required.');
 const image=be(data,4),nrel=be(data,8),sites=be(data,12),init=be(data,16),check=be(data,20),off=be(data,24),size=be(data,28),start=32+4*(nrel+sites),end=start+image;
 if(!sites||sites>256||end>data.length||init>=image||(end-32)%4)fail('Invalid mods file bounds.');
 if(sum(data,32,end)!==check)fail('Mods file checksum mismatch.');
 for(let i=0;i<sites;i++){const x=be(data,32+4*i);if(x!==0xffffffff&&x>=image)fail('Invalid mods entry.');}
 for(let i=0;i<nrel;i++){const x=be(data,32+4*(sites+i));if(x%2||x+4>image)fail('Invalid mods relocation.');}
 if(size){if(off<end||off+size>data.length||size<16||be(data,off)!==0x534d4133)fail('Invalid mods audio section.');const count=be(data,off+4);if(be(data,off+8)!==size||count>16||16+count*16>size)fail('Invalid audio table.');}
 return {format:magic,sites,image_bytes:image};
}
export function packMods(data){
 const obj=new Uint8Array(43+data.length),v=new DataView(obj.buffer);v.setUint32(0,0xac11d303);obj[4]=2;v.setUint16(5,8);v.setUint16(7,13);obj.set([48,48,57,52],9);v.setUint32(13,8);v.setUint32(17,0xffffffff);v.setUint32(21,1);v.setUint32(25,data.length);obj[30]=12;obj.set(data,31);v.setUint32(31+data.length,deviceCrc(data));v.setUint32(35+data.length,data.length);v.setUint32(39+data.length,0xaaa1daaa);return obj;
}
export function unpackMods(obj){
 validateObject(obj);if(obj.length<43||obj[4]!==2||obj[5]!==0||obj[6]!==8||be(obj,13)!==8||be(obj,21)!==1||obj[29]!==0||obj[30]!==12)fail('Invalid /mods Data object header.');
 const body=obj.subarray(31,-12);if(body.length>16*1024*1024||be(obj,25)!==body.length)fail('Invalid /mods body length.');return body;
}
export async function uploadMods(rpc,data,progress){const info=inspectMods(data);await writeObject(rpc,'/mods',packMods(data),progress,8192,back=>{const body=unpackMods(back);if(body.length!==data.length||body.some((byte,i)=>byte!==data[i]))fail('Mods file verification failed.');},0);return info;}
export function validateSysex(data){
 let p=0,count=0;while(p<data.length){if(data[p]!==0xf0||data[p+1]!==0||data[p+2]!==0x20||data[p+3]!==0x3c)fail('Unsupported SysEx message.');let end=p+1;while(end<data.length&&data[end]!==0xf7){if(data[end]>127)fail('Invalid SysEx message.');end++;}if(end===data.length)fail('Incomplete SysEx message.');p=end+1;count++;}if(!count)fail('Empty SysEx file.');
}
export async function uploadFirmware(rpc,data,progress=()=>{}){
 validateSysex(data);const length=Uint8Array.of(data.length&255,(data.length>>>8)&255,(data.length>>>16)&255,(data.length>>>24)&255),start=await rpc.call(0x50,concat(length,new TextEncoder().encode('sysex'),Uint8Array.of(0,1)),10000);
 if(start.length<6||start[5]!==0)fail('The device did not accept the firmware.');
 for(let offset=0;offset<data.length;offset+=2048){const chunk=data.subarray(offset,offset+2048),rx=await rpc.call(0x51,concat(u32(deviceCrc(chunk)),u32(chunk.length),u32(offset),chunk),15000);if(rx.length<10||rx[9]>1||(rx[9]===1&&offset+chunk.length<data.length))fail('Firmware transfer failed or ended early.');progress(offset+chunk.length,data.length,'Uploading firmware');await new Promise(r=>setTimeout(r,50));}
 return {ok:true,bytes:data.length,installed:null};
}
