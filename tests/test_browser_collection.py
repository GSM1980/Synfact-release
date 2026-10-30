"""Exercise grouped downloads with a simulated MIDI device and inspect ZIPs."""
import base64
import io
import json
from pathlib import Path
import subprocess
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = r"""
import {downloadCollection} from './synfact-transfer/browser/collection.mjs';
import {deviceCrc} from './synfact-transfer/browser/native-engine.mjs';
const obj=new Uint8Array(46),v=new DataView(obj.buffer);v.setUint32(0,0xac11d303);obj[8]=13;v.setUint32(25,3);obj[30]=12;obj.set([1,2,3],31);v.setUint32(34,deviceCrc(obj.subarray(31,34)));v.setUint32(38,3);v.setUint32(42,0xaaa1daaa);
let opens=0;const mode=process.argv[1];
const rpc={async call(type){if(type===0x54){opens++;if(mode==='fail'&&opens>=2)throw Error('lost');return Uint8Array.of(0,0,0,0,0xd4,1,0,0,0,7,0,0,8,0,0);}
if(type===0x56)return Uint8Array.of(0,0,0,0,0xd6,1);
return Uint8Array.of(0,0,0,0,0xd5,1,0,0,0,7,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,obj.length,...obj);}};
const items=[{path:'/projects/1',name:'My project',bytes:46},{path:'/soundbanks/A/2',name:'Lead',bytes:46}];
try{const blob=await downloadCollection(rpc,items,{cancelled:()=>mode==='cancel'&&opens>=1});
if(mode!=='ok')throw Error('Unexpected success');process.stdout.write(Buffer.from(await blob.arrayBuffer()).toString('base64'));
}catch(e){if(mode==='ok')throw e;if(!/cancelled|lost/.test(e.message))throw e;process.stdout.write('rejected');}
"""


class BrowserCollectionTests(unittest.TestCase):
    def run_case(self, mode):
        result = subprocess.run(['node', '--input-type=module', '-e', SCRIPT, mode], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_zip_contains_individually_importable_files_and_manifest(self):
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(self.run_case('ok')))) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.namelist(), ['001-My project.stprj', '002-Lead.stsnd', 'collection.json'])
            records = json.loads(archive.read('collection.json'))
            self.assertEqual([r['path'] for r in records], ['/projects/1', '/soundbanks/A/2'])
            for record, kind in zip(records, ('Project', 'Sound')):
                with zipfile.ZipFile(io.BytesIO(archive.read(record['file']))) as entry:
                    self.assertIsNone(entry.testzip())
                    self.assertEqual(json.loads(entry.read('manifest.json'))['FileType'], kind)
                    self.assertEqual(len(entry.read('payload')), 46)

    def test_failure_and_cancellation_do_not_return_an_archive(self):
        self.assertEqual(self.run_case('fail'), 'rejected')
        self.assertEqual(self.run_case('cancel'), 'rejected')


if __name__ == '__main__':
    unittest.main()
