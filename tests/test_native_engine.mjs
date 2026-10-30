import {test} from 'node:test';
import assert from 'node:assert/strict';
import {listDirectory,deviceCrc,validateObject,packageObject,readObject,readPackage,writeObject,organize} from '../synfact-transfer/browser/native-engine.mjs';

test('lists and parses Syntakt DataList entries',async()=>{
 let request;
 const rpc={async call(type,payload){request={type,payload};return Uint8Array.of(
   0,1,0,1,0xd3,1,...Array(12).fill(0),
   84,69,83,84,0,0,2,0,0,0,3,0,0,0,42,0,8,1,0);}};
 const result=await listDirectory(rpc,'/projects');
 assert.equal(request.type,0x53);
 assert.equal(new TextDecoder().decode(request.payload.subarray(0,10)),'/projects\0');
 assert.equal(result.entries[0].name,'TEST');
 assert.equal(result.entries[0].slot,3);
 assert.equal(result.entries[0].size,42);
 assert.equal(result.entries[0].path,'/projects/3');
});
test('object integrity, download framing, and ZIP package',async()=>{
 const body=Uint8Array.of(1,2,3),obj=new Uint8Array(46),view=new DataView(obj.buffer);
 view.setUint32(0,0xac11d303);obj[8]=13;view.setUint32(25,3);obj[30]=12;obj.set(body,31);view.setUint32(34,deviceCrc(body));view.setUint32(38,3);view.setUint32(42,0xaaa1daaa);
 assert.equal(deviceCrc(new TextEncoder().encode('123456789')),0xd202d277);
 assert.deepEqual(validateObject(obj),obj);
 const calls=[];const rpc={async call(type,payload){calls.push(type);if(type===0x54)return Uint8Array.of(0,0,0,0,0,1,0,0,0,7,0,0,8,0,0);if(type===0x55)return Uint8Array.of(0,0,0,0,0,1,0,0,0,7,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,obj.length,...obj);return Uint8Array.of(0,0,0,0,0,1);}};
 assert.deepEqual(await readObject(rpc,'/projects/1'),obj);assert.deepEqual(calls,[0x54,0x55,0x56]);
 const zip=packageObject(obj,'Project');assert.equal(new DataView(zip.buffer).getUint32(0,true),0x04034b50);
 assert.deepEqual(await readPackage(zip,'Project'),obj);
 await assert.rejects(readPackage(zip,'Sound'),/incompatible/);
 let seq=0,sent=0;const writer={async call(type,payload){if(type===0x57)return Uint8Array.of(0,0,0,0,0,1,0,0,0,7);if(type===0x58){sent+=payload.length-16;return Uint8Array.of(0,0,0,0,0,1,0,0,0,7,0,0,0,seq++,0,0,0,sent);}if(type===0x59)return Uint8Array.of(0,0,0,0,0,1,0,0,0,7,0,0,0,obj.length);return rpc.call(type,payload);}};
 assert.deepEqual(await writeObject(writer,'/projects/1',obj),{ok:true});
 obj[32]^=1;assert.throws(()=>validateObject(obj),/CRC/);
});
test('rejects malformed device responses and unsafe paths',async()=>{
 await assert.rejects(listDirectory({call:async()=>Uint8Array.of(0,0,0,0,0,1,...Array(12).fill(0),65)},'/projects'),/Unterminated/);
 await assert.rejects(listDirectory({call:async()=>{}},'/projects/../mods'),/Unsupported/);
});
test('read accepts device response counters that differ from request and empty partials',async()=>{
 const obj=new Uint8Array(46),v=new DataView(obj.buffer);v.setUint32(0,0xac11d303);obj[8]=13;v.setUint32(25,3);obj[30]=12;obj.set([1,2,3],31);v.setUint32(34,deviceCrc(obj.subarray(31,34)));v.setUint32(38,3);v.setUint32(42,0xaaa1daaa);
 let count=0;const requests=[];
 const rpc={async call(type,payload){if(type===0x54)return Uint8Array.of(0,0,0,0,0xd4,1,0,0,0,7,0,0,8,0,0);if(type===0x56)return Uint8Array.of(0,0,0,0,0xd6,1);requests.push(new DataView(payload.buffer,payload.byteOffset,payload.byteLength).getUint32(4));const chunk=count++===0?new Uint8Array():obj;return Uint8Array.of(0,0,0,0,0xd5,1,0,0,0,7,0,0,0,55,0,0,0,0,chunk.length?1:0,0,0,0,0,0,0,0,chunk.length,...chunk);}};
 assert.deepEqual(await readObject(rpc,'/soundbanks/A/1'),obj);assert.deepEqual(requests,[0,1]);
});
test('large reads retry with smaller blocks after corruption and retain CRC validation',async()=>{
 const obj=new Uint8Array(46),v=new DataView(obj.buffer);v.setUint32(0,0xac11d303);obj[8]=13;v.setUint32(25,3);obj[30]=12;obj.set([1,2,3],31);v.setUint32(34,deviceCrc(obj.subarray(31,34)));v.setUint32(38,3);v.setUint32(42,0xaaa1daaa);
 const blocks=[],updates=[];let attempt=0,closes=0;
 const rpc={async call(type,payload){
  if(type===0x54){attempt++;blocks.push(new DataView(payload.buffer,payload.byteOffset+payload.length-5,4).getUint32(0));return Uint8Array.of(0,0,0,0,0xd4,1,0,0,0,7,0,0,8,0,0);}
  if(type===0x56){closes++;return Uint8Array.of(0,0,0,0,0xd6,1);}
  const chunk=obj.slice();if(attempt===1)chunk[32]^=1;
  return Uint8Array.of(0,0,0,0,0xd5,1,0,0,0,7,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,chunk.length,...chunk);
 }};
 assert.deepEqual(await readObject(rpc,'/projects/1',(...p)=>updates.push(p)),obj);
 assert.deepEqual(blocks,[8192,2048]);assert.equal(closes,2);
 assert.ok(updates.some(p=>p[2]==='Retrying with smaller blocks'));
 assert.ok(updates.some(p=>p[1]===obj.length));
});
test('cancelled reads do not start a fallback transfer',async()=>{
 let opens=0;const rpc={closed:false,async call(type){if(type===0x54){opens++;this.closed=true;throw Error('closed');}}};
 await assert.rejects(readObject(rpc,'/projects/1'),/closed/);assert.equal(opens,1);
});
test('organization checks capabilities and sends the expected device requests',async()=>{
 let mask=0x7c;const calls=[];
 const rpc={async call(type,payload){calls.push({type,payload});if(type===0x53)return Uint8Array.of(0,0,0,0,0xd3,1,...Array(12).fill(0),65,0,0,2,0,0,0,3,0,0,0,5,0,mask,1,0);return Uint8Array.of(0,0,0,0,type|0x80,1);}};
 for(const [action,type] of [['clear',0x5c],['copy',0x5b],['move',0x5a],['swap',0x5d],['rename',0x5e]]){
  calls.length=0;await organize(rpc,action,{path:'/projects/3',destination:'/projects/4',name:'Café',confirm:true});
  assert.deepEqual(calls.map(c=>c.type),[0x53,type]);
  const data=calls[1].payload;assert.equal(new TextDecoder().decode(data.subarray(0,12)),'/projects/3\0');
  if(action==='rename')assert.deepEqual([...data.slice(12)],[67,97,102,233,0]);
 }
 mask=0;calls.length=0;await assert.rejects(organize(rpc,'clear',{path:'/projects/3',confirm:true}),/does not support/);assert.deepEqual(calls.map(c=>c.type),[0x53]);
 for(const args of [{path:'/projects/3',destination:'/soundbanks/A/4',confirm:true},{path:'/projects/3',destination:'/projects/3',confirm:true},{path:'/samples/3',destination:'/samples/4',confirm:true}])await assert.rejects(organize(rpc,'copy',args));
 await assert.rejects(organize(rpc,'rename',{path:'/projects/3',name:'🎹',confirm:true}),/Windows-1252/);
 await assert.rejects(organize(rpc,'clear',{path:'/projects/3'}),/Confirm/);
 mask=0x7c;const flaky={async call(type,payload){if(type===0x53)return rpc.call(type,payload);throw Error('timeout');}};
 await assert.rejects(organize(flaky,'clear',{path:'/projects/3',confirm:true}),/may have completed/);
});
