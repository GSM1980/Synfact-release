"""Offline parity against the desktop WAV and stock-sample implementation."""
import base64
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'synfact-transfer' / 'vendor'))
import hzfs
import stfiles


class BrowserSamples(unittest.TestCase):
    def test_wav_and_object_parity(self):
        vectors = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sample.wav'
            for rate in (24000, 44100, 48000):
                for channels in (1, 2):
                    pcm = b''.join(struct.pack('<h', ((i * 7919) % 65536) - 32768) for i in range(60 * channels))
                    fmt = struct.pack('<HHIIHH', 1, channels, rate, rate * channels * 2, channels * 2, 16)
                    data = b'WAVEfmt ' + struct.pack('<I', 16) + fmt + b'data' + struct.pack('<I', len(pcm)) + pcm
                    wav = b'RIFF' + struct.pack('<I', len(data)) + data
                    path.write_bytes(wav)
                    source_rate, mono = hzfs.read_wav(path)
                    expected = hzfs.to_pcm16be(hzfs.resample(mono, source_rate))
                    obj = b''.join(stfiles.build_sample(expected, 4, 'TEST'))
                    hzfs.write_wav(path, expected)
                    vectors.append({'wav': base64.b64encode(wav).decode(), 'pcm': base64.b64encode(expected).decode(),
                                    'obj': base64.b64encode(obj).decode(), 'export': base64.b64encode(path.read_bytes()).decode()})
        script = """import {readWav,buildSample,sampleWav} from './synfact-transfer/browser/samples.mjs';
let s='';for await(const c of process.stdin)s+=c;
for(const [i,v] of JSON.parse(s).entries()) {const pcm=readWav(Uint8Array.from(Buffer.from(v.wav,'base64')));
if(Buffer.from(pcm).toString('base64')!==v.pcm)throw Error('PCM mismatch '+i);
const obj=buildSample(pcm,4,'TEST');if(Buffer.from(obj).toString('base64')!==v.obj)throw Error('Object mismatch '+i);
const wav=sampleWav(obj);if(Buffer.from(wav).toString('base64')!==v.export)throw Error('WAV mismatch '+i);}
"""
        result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT,
                                input=json.dumps(vectors), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
