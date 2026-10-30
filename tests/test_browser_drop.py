"""Compare browser mods validation with the desktop reference, without MIDI."""
import base64
import json
from pathlib import Path
import struct
import subprocess
import sys
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_transfer_mods import bank
import core


class BrowserDropTests(unittest.TestCase):
    def test_mod_validation_and_framing_match_desktop(self):
        good = bank()
        vectors = [good, b'invalid', b'SMZ4' + struct.pack('>7I', 4, 0, 1, 0, 0, 0, 0) + bytes(8)]
        for offset, value in ((8, 0), (24, 8), (36, 17), (40, 0xfffffffc), (52, 0xfffffffc), (60, 1), (68, 1), (80, 1)):
            changed = bytearray(good)
            struct.pack_into('>I', changed, offset, value)
            struct.pack_into('>I', changed, 16, zlib.adler32(changed[20:]))
            vectors.append(bytes(changed))
        cases = []
        for data in vectors:
            try:
                core.machine_info(data)
                accepted = True
            except ValueError:
                accepted = False
            cases.append({'data': base64.b64encode(data).decode(), 'ok': accepted,
                          'object': base64.b64encode(core.M.pack(data)).decode()})
        script = """import {inspectMods,packMods} from './synfact-transfer/browser/drop.mjs';
let s='';for await(const c of process.stdin)s+=c;
for(const [i,v] of JSON.parse(s).entries()){const bytes=Uint8Array.from(Buffer.from(v.data,'base64'));let ok=true;try{inspectMods(bytes)}catch{ok=false}
if(ok!==v.ok)throw Error('Validation mismatch '+i);if(Buffer.from(packMods(bytes)).toString('base64')!==v.object)throw Error('Framing mismatch '+i);}"""
        result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT,
                                input=json.dumps(cases), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mod_upload_readback_and_mismatch(self):
        script = """import {uploadMods} from './synfact-transfer/browser/drop.mjs';
let s='';for await(const c of process.stdin)s+=c;const data=Uint8Array.from(Buffer.from(s,'base64'));
let chunks=[],object,readJob=2,seq=0,bad=false;const calls=[];
const be=n=>[(n>>>24)&255,(n>>>16)&255,(n>>>8)&255,n&255];
const rpc={async call(type,p){calls.push(type);const ok=[0,0,0,0,type|128,1];if(type===0x57){chunks=[];return Uint8Array.of(...ok,...be(1));}
if(type===0x58){chunks.push(...p.subarray(16));return Uint8Array.of(...ok,...be(1),...be(seq++),...be(chunks.length));}
if(type===0x59){object=Uint8Array.from(chunks);return Uint8Array.of(...ok,...be(1),...be(object.length));}
if(type===0x54)return Uint8Array.of(...ok,...be(readJob),...be(8192),0);
if(type===0x55){const bytes=object.slice();if(bad)bytes[31]^=1;return Uint8Array.of(...ok,...be(readJob),...be(0),...be(0),1,...be(0),...be(bytes.length),...bytes);}
if(type===0x56)return Uint8Array.of(...ok);throw Error('unexpected');}};
await uploadMods(rpc,data,()=>{});if(!calls.includes(0x54)||!calls.includes(0x59))throw Error('Missing readback');
bad=true;seq=0;let failed=false;try{await uploadMods(rpc,data,()=>{})}catch{failed=true}if(!failed)throw Error('Corrupt readback accepted');"""
        result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT,
                                input=base64.b64encode(bank()).decode(), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_large_mod_upload_uses_8k_chunks_and_verifies(self):
        script = """import {uploadMods} from './synfact-transfer/browser/drop.mjs';
import {deviceCrc} from './synfact-transfer/browser/native-engine.mjs';
let s='';for await(const c of process.stdin)s+=c;const data=Uint8Array.from(Buffer.from(s,'base64'));
const be=n=>[(n>>>24)&255,(n>>>16)&255,(n>>>8)&255,n&255];let parts=[],size=0,object,readBlock,writeSizes=[];
const rpc={async call(type,p){const ok=[0,0,0,0,type|128,1],v=new DataView(p.buffer,p.byteOffset,p.byteLength);
if(type===0x57)return Uint8Array.of(...ok,...be(1));
if(type===0x58){const seq=v.getUint32(4),n=v.getUint32(12),chunk=p.subarray(16);if(chunk.length!==n||v.getUint32(8)!==deviceCrc(chunk))throw Error('Bad chunk');parts.push(chunk.slice());size+=n;writeSizes.push(n);return Uint8Array.of(...ok,...be(1),...be(seq),...be(size));}
if(type===0x59){object=new Uint8Array(size);let at=0;for(const part of parts){object.set(part,at);at+=part.length;}return Uint8Array.of(...ok,...be(1),...be(size));}
if(type===0x54){readBlock=v.getUint32(p.length-5);return Uint8Array.of(...ok,...be(2),...be(readBlock),0);}
if(type===0x55){const seq=v.getUint32(4),chunk=object.subarray(seq*readBlock,(seq+1)*readBlock);return Uint8Array.of(...ok,...be(2),...be(seq),...be(0),Number((seq+1)*readBlock>=object.length),...be(0),...be(chunk.length),...chunk);}
if(type===0x56)return Uint8Array.of(...ok);throw Error('unexpected');}};
await uploadMods(rpc,data,()=>{});if(writeSizes[0]!==8192||writeSizes.at(-1)!==12||writeSizes.some(n=>n>8192))throw Error('Invalid chunk sizes');"""
        result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT,
                                input=base64.b64encode(bank(0x81000)).decode(), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
