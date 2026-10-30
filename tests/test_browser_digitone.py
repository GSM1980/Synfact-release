"""Compare the browser converter byte-for-byte with the desktop reference."""
import base64
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_transfer_digitone import sound
import digitone


class BrowserDigitoneParity(unittest.TestCase):
    def test_versions_and_parameter_boundaries(self):
        vectors = []
        for version in range(1, 7):
            for ratio in (0, 127, 720):
                for attack in (0, 0x800, 0x2345):
                    obj = sound(version=version, **{'20': ratio, '66': attack, '54': 0x300})
                    vectors.append({'in': base64.b64encode(obj).decode(),
                                    'out': base64.b64encode(digitone.convert(obj, 17)[0]).decode()})
        script = """import {convertDigitone} from './synfact-transfer/browser/digitone.mjs';
let s='';for await(const c of process.stdin)s+=c;
for(const [i,v] of JSON.parse(s).entries()){
 const got=Buffer.from(convertDigitone(Uint8Array.from(Buffer.from(v.in,'base64')),17).object).toString('base64');
 if(got!==v.out)throw Error('Mismatch vector '+i);
}console.log('Digitone parity passed');"""
        result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT,
                                input=json.dumps(vectors), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_deflated_transfer_package(self):
        obj = sound(version=6)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('manifest.json', json.dumps({'FileType': 'Sound', 'ProductType': ['24'], 'Payload': 'sound'}))
            z.writestr('sound', obj)
        script = """import {readPackage} from './synfact-transfer/browser/native-engine.mjs';
import {convertDigitone} from './synfact-transfer/browser/digitone.mjs';
let s='';for await(const c of process.stdin)s+=c;
const obj=await readPackage(Uint8Array.from(Buffer.from(s,'base64')),'Sound',{digitone:true});
process.stdout.write(Buffer.from(convertDigitone(obj).object).toString('base64'));"""
        result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT,
                                input=base64.b64encode(archive.getvalue()).decode(), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(base64.b64decode(result.stdout), digitone.convert(obj)[0])


if __name__ == '__main__':
    unittest.main()
