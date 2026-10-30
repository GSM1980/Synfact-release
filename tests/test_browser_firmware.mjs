import {test} from 'node:test';
import assert from 'node:assert/strict';
import {uploadFirmware,validateSysex} from '../synfact-transfer/browser/drop.mjs';
import {deviceCrc} from '../synfact-transfer/browser/native-engine.mjs';

test('firmware frames are validated before MIDI and chunks follow protocol',async()=>{
 const bytes=Uint8Array.of(0xf0,0,0x20,0x3c,...new Uint8Array(3000),0xf7),calls=[];
 const rpc={async call(type,payload){calls.push({type,payload});return type===0x50?Uint8Array.of(0,0,0,0,0,0):Uint8Array.of(0,0,0,0,0,0,0,0,0,0);}};
 const result=await uploadFirmware(rpc,bytes);assert.equal(result.installed,null);assert.deepEqual(calls.map(c=>c.type),[0x50,0x51,0x51]);
 assert.equal(new DataView(calls[0].payload.buffer).getUint32(0,true),bytes.length);
 const payload=calls[1].payload;assert.equal(new DataView(payload.buffer,payload.byteOffset).getUint32(0),deviceCrc(payload.subarray(12)));
 await assert.rejects(uploadFirmware(rpc,Uint8Array.of(0xf0,0,0x20,0x3c,0x80,0xf7)),/Invalid/);assert.equal(calls.length,3);
 assert.throws(()=>validateSysex(Uint8Array.of(0xf0,0,0x20,0x3c)),/Incomplete/);
});
test('firmware early termination aborts remaining chunks',async()=>{
 let calls=0;const rpc={async call(type){calls++;return type===0x50?Uint8Array.of(0,0,0,0,0,0):Uint8Array.of(0,0,0,0,0,0,0,0,0,1);}};
 await assert.rejects(uploadFirmware(rpc,Uint8Array.of(0xf0,0,0x20,0x3c,...new Uint8Array(3000),0xf7)),/ended early/);assert.equal(calls,2);
});
