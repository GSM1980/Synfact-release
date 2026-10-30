import {readObject,packageObject} from './native-engine.mjs';

const le16=n=>Uint8Array.of(n&255,(n>>>8)&255);
const le32=n=>Uint8Array.of(n&255,(n>>>8)&255,(n>>>16)&255,(n>>>24)&255);
function concat(...parts){const data=new Uint8Array(parts.reduce((n,p)=>n+p.length,0));let offset=0;for(const part of parts){data.set(part,offset);offset+=part.length;}return data;}
function crc32(data){let crc=0xffffffff;for(const byte of data){crc^=byte;for(let i=0;i<8;i++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}return (crc^0xffffffff)>>>0;}
function storedZip(entries){
 const local=[],central=[];let offset=0;
 for(const {name,data} of entries){
  const filename=new TextEncoder().encode(name),crc=crc32(data),size=data.length,flags=0x800;
  const header=concat(le32(0x04034b50),le16(20),le16(flags),le16(0),le16(0),le16(0),le32(crc),le32(size),le32(size),le16(filename.length),le16(0),filename);
  central.push(concat(le32(0x02014b50),le16(20),le16(20),le16(flags),le16(0),le16(0),le16(0),le32(crc),le32(size),le32(size),le16(filename.length),le16(0),le16(0),le16(0),le16(0),le32(0),le32(offset),filename));
  local.push(header,data);offset+=header.length+size;
 }
 const directory=concat(...central),end=concat(le32(0x06054b50),le16(0),le16(0),le16(entries.length),le16(entries.length),le32(directory.length),le32(offset),le16(0));
 return new Blob([...local,directory,end],{type:'application/zip'});
}
export async function downloadCollection(rpc,items,{progress=()=>{},cancelled=()=>false}={}){
 if(!Array.isArray(items)||items.length<1||items.length>256)throw Error('Select between 1 and 256 projects or sounds.');
 for(const item of items)if(!item||typeof item.path!=='string'||!/^\/(?:projects\/(?:[1-9]|[1-9]\d|1[01]\d|12[0-8])|soundbanks\/[A-H]\/(?:[1-9]|[1-9]\d|1\d\d|2[0-4]\d|25[0-6]))$/.test(item.path))throw Error('Select projects or sounds only.');
 const entries=[],records=[];let size=0;
 for(let i=0;i<items.length;i++){
  if(cancelled())throw Error('Download cancelled.');
  const item=items[i],kind=item.path.startsWith('/projects/')?'Project':'Sound';
  const object=await readObject(rpc,item.path,(n,total,phase)=>progress(n,total,`${phase} · ${i+1}/${items.length}`));
  if(cancelled())throw Error('Download cancelled.');
  const data=packageObject(object,kind);size+=data.length;if(size>512*1024*1024)throw Error('The selection exceeds 512 MB. Select fewer files.');
  const name=(String(item.name||'backup').replace(/[^\w. -]/g,'_').slice(0,100)||'backup');
  const file=String(i+1).padStart(3,'0')+'-'+name+(kind==='Project'?'.stprj':'.stsnd');
  entries.push({name:file,data});records.push({...item,file});
 }
 if(cancelled())throw Error('Download cancelled.');
 entries.push({name:'collection.json',data:new TextEncoder().encode(JSON.stringify(records,null,2))});
 return storedZip(entries);
}
