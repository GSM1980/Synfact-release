"""Synfact Transfer: device operations, serialized by the server worker.

No MIDI is opened or sent on import.
"""
from pathlib import Path
import base64
import contextlib
import io
import json
import re
import struct
import sys
import tempfile
import time
import zipfile
import zlib
import hashlib

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'vendor'))
import stfiles as S
import hzfs as H
import emmc_dump as D
import files_host as F
import elektron_object as E
import compaction
import digitone
from midi_io import MidiDevice, ConnectionLost, ports

# The pinned RtMidi 1.5.8 cannot enlarge WinMM's SysEx buffers. Hardware reads
# validated 2 KiB with no fixed delay; larger blocks suffered truncation.
# Write pacing stays independent of read benchmarks.
READ_BLOCK = 2048
READ_REST = 0.0
WRITE_REST = 0.05


class Cancelled(S.DeviceError):
    retryable = False


def validate_object(obj, product=13):
    if len(obj) < 43 or obj[:4] != bytes.fromhex('ac11d303'):
        raise ValueError('Invalid device object.')
    body, footer = obj[31:-12], obj[-12:]
    if obj[30] != 12 or S.u32(footer, 8) != 0xAAA1DAAA:
        raise ValueError('Invalid object footer.')
    if S.u32(footer, 4) != len(body) or S.u32(footer, 0) != S.ecrc(body):
        raise ValueError('Object size or CRC mismatch.')
    if not obj[29] and S.u32(obj, 25) != len(body):
        raise ValueError('Invalid declared size.')
    if obj[29]:
        E.block_chain(body)
    if int.from_bytes(obj[7:9], 'big') != product:
        raise ValueError('This object is not compatible with the connected device.')
    return obj


def read_object(dev, path, *, log=None):
    """Fast USB read, with one conservative restart on incomplete/corrupt data."""
    log = log or getattr(dev, 'log', lambda text: None)
    for attempt, (block, rest) in enumerate(((READ_BLOCK, READ_REST), (2048, 0.05))):
        try:
            return validate_object(S.data_read(dev, path, block=block, rest=rest, progress=False))
        except Cancelled:
            raise
        except (S.DeviceError, ValueError) as error:
            if attempt:
                raise
            log(f'Reading again with conservative settings: {error}')


def read_package(data, *, allow_digitone=False, filename='', index=0, log=lambda text: None):
    if data[:2] != b'PK':
        return {}, validate_object(data)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if sum(i.file_size for i in z.infolist()) > 64 * 1024 * 1024:
            raise ValueError('Package is too large.')
        manifest = json.loads(z.read('manifest.json'))
        products = set(manifest.get('ProductType', []))
        obj = z.read(manifest['Payload'])
        # The extension selects Digitone conversion regardless of manifest IDs.
        # Without a filename, use the internal device ID: Transfer exports can
        # list shared ProductType values such as ['24', '30'].
        if allow_digitone and (filename.lower().endswith('.dnsnd')
                               or int.from_bytes(obj[7:9], 'big') == 9):
            if manifest.get('FileType') != 'Sound':
                raise ValueError('Only Digitone sound files can be converted.')
            obj = validate_object(obj, product=9)
            if len(obj) > 65536 or S.u32(obj, 25) not in (0x11B, 0x11A, 0x12E):
                raise ValueError('Invalid Digitone sound object size.')
            obj, notes = digitone.convert(obj, index)
            for note in notes:
                log(note)
            return {**manifest, 'ProductType': ['30']}, validate_object(obj)
        if not products & {'30'}:
            raise ValueError('Incompatible package type.')
        return manifest, validate_object(obj)


def package(obj, name, kind):
    validate_object(obj)
    payload = 'payload'
    manifest = {'FormatVersion': '1.0', 'ProductType': ['30'],
                'FileType': kind, 'Payload': payload,
                'FirmwareVersion': '1.41'}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json', json.dumps(manifest, indent=2))
        z.writestr(payload, obj)
    return buf.getvalue()


def device_path(path):
    if not isinstance(path, str) or '\0' in path:
        raise ValueError('Invalid path.')
    if re.fullmatch(r'/samples/(?:[1-9]|[1-5][0-9]|6[0-4])', path):
        return path
    if re.fullmatch(r'/projects/(?:[1-9]|[1-9][0-9]|1[01][0-9]|12[0-8])', path):
        return path
    if re.fullmatch(r'/soundbanks/[A-H]/(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-6])', path):
        return path
    raise ValueError('Path is outside projects, sounds or stock samples.')


def directory(path):
    if path in ('/', '/projects', '/samples', '/soundbanks') or re.fullmatch(r'/soundbanks/[A-H]', path):
        return path
    raise ValueError('Invalid folder.')


def machine_info(data):
    if len(data) > F.SLOT_BYTES - F.HEADER:
        raise ValueError('The mods file exceeds the mods slot size.')
    if data[:4] == b'SAFE':
        if len(data) < 48:
            raise ValueError('Incomplete SAFE mods header.')
        _, version, abi, length, checksum, zone, patches, count, relocations, nrel, slots, reserved = struct.unpack_from('>12I', data)
        if version != 1 or length != len(data) or reserved or zone != 48:
            raise ValueError('Invalid SAFE mods header.')
        if zlib.adler32(data[20:]) & 0xffffffff != checksum:
            raise ValueError('Mods file integrity check failed.')
        if not 1 <= count <= 256 or patches < zone + 32 or patches % 4 or patches + count * 16 > relocations:
            raise ValueError('Invalid mods patch table.')
        if relocations % 4 or relocations + nrel * 4 != len(data):
            raise ValueError('Invalid mods relocation table.')
        for i in range(count):
            address, size, before, after = struct.unpack_from('>4I', data, patches + i * 16)
            if not size or address % 2 or any(offset < patches + count * 16 or offset + size > relocations for offset in (before, after)):
                raise ValueError('Invalid mods patch bounds.')
        for i in range(nrel):
            offset = S.u32(data, relocations + i * 4)
            if offset % 2 or offset < patches + count * 16 or offset + 4 > relocations:
                raise ValueError('Invalid mods import relocation.')
        if data[zone:zone + 4] == b'SAFE':
            raise ValueError('Nested mods envelopes are not supported.')
        info = machine_info(data[zone:patches])
        if slots >= info['image_bytes']:
            raise ValueError('Invalid mods export table offset.')
        return {**info, 'format': 'SAFE', 'abi': abi, 'bytes': len(data),
                'sha256': hashlib.sha256(data).hexdigest()}
    magic = data[:4]
    zone_magic = magic == b'SMZ4' or re.fullmatch(rb'S[0-9]{1,2}Z[0-9]?', magic) is not None
    if len(data) < 32 or not zone_magic:

        raise ValueError('A valid mods file fitting in the mods slot is required.')
    image, nrel, nsites, init, check, off, size = struct.unpack_from('>7I', data, 4)
    start, end = 32 + 4 * (nrel + nsites), 32 + 4 * (nrel + nsites) + image
    if not nsites or nsites > 256 or end > len(data) or init >= image or (end - 32) % 4:
        raise ValueError('Invalid mods file bounds.')
    total = sum(struct.unpack_from('>I', data, p)[0] for p in range(32, end, 4)) & 0xFFFFFFFF
    if total != check:
        raise ValueError('Mods file checksum mismatch.')
    entries = struct.unpack_from(f'>{nsites}I', data, 32)
    if any(v != 0xFFFFFFFF and v >= image for v in entries):
        raise ValueError('Mods entry is outside the image.')
    relocs = struct.unpack_from(f'>{nrel}I', data, 32 + 4 * nsites)
    if any(v % 2 or v + 4 > image for v in relocs):
        raise ValueError('Invalid mods relocation.')
    types = []
    if size:
        if off < end or off + size > len(data) or size < 16 or data[off:off + 4] != b'SMA3':
            raise ValueError('Invalid mods audio section.')
        count, declared = struct.unpack_from('>2I', data, off + 4)
        if declared != size or count > 16 or 16 + count * 16 > size:
            raise ValueError('Invalid audio table.')
        types = [S.u32(data, off + 16 + 16 * i) for i in range(count)]
    return {'bytes': len(data), 'sites': nsites, 'machines': types, 'image_bytes': image, 'format': magic.decode('ascii'),
            'sha256': hashlib.sha256(data).hexdigest(), 'path': '/samples/449'}


class Transport:
    def __init__(self, dev, log, cancelled, expected=0):
        self.dev, self.log, self.cancelled = dev, log, cancelled
        self.total, self.received = expected or 0, 0
        self.verifying = False
        self.phase = None
        self.phase_started = 0
        self.phase_current = 0
        self.activity_label = None

    @contextlib.contextmanager
    def activity(self, label):
        previous = self.activity_label
        self.activity_label = label
        self.report(0, 0, label)
        try:
            yield
        finally:
            self.activity_label = previous

    def report(self, current, total, phase):
        now = time.monotonic()
        if phase != self.phase or current < self.phase_current or current == 0:
            self.phase, self.phase_started = phase, now
        self.phase_current = current
        elapsed = max(0, now - self.phase_started)
        self.log('@progress ' + json.dumps({'current': current, 'total': total, 'phase': phase,
                                            'elapsed_seconds': elapsed,
                                            'kib_s': current / 1024 / elapsed if elapsed else 0}))

    def finish_phase(self):
        if self.phase_current:
            elapsed = time.monotonic() - self.phase_started
            if elapsed > 0:
                self.log(f'{self.phase}: {self.phase_current / 1024:.1f} KiB in '
                         f'{elapsed:.2f} s ({self.phase_current / 1024 / elapsed:.1f} KiB/s).')

    def call(self, t, payload=b'', timeout=None):
        if self.cancelled() and t not in (0x56, 0x59):
            raise Cancelled('Transfer cancelled.')
        rx = self.dev.call(t, payload, timeout)
        if rx is not None:
            if t == 0x57 and len(payload) >= 4:
                self.total = S.u32(payload, 0)
                self.verifying = True
                self.report(0, self.total, self.activity_label or 'Uploading')
            elif t == 0x58 and len(rx) >= 18:
                self.report(S.u32(rx, 14), self.total, self.activity_label or 'Uploading')
            elif t == 0x54:
                self.received = 0
                self.report(0, 0 if self.activity_label else self.total,
                            self.activity_label or ('Verifying' if self.verifying else 'Downloading'))
            elif t == 0x55 and len(rx) >= 27:
                self.received += S.u32(rx, 23)
                total = self.total if self.verifying else (self.received if rx[18] else 0)
                self.report(self.received, 0 if self.activity_label else total,
                            self.activity_label or ('Verifying' if self.verifying else 'Downloading'))
            elif t in (0x56, 0x59) and len(rx) >= 6 and rx[5]:
                self.finish_phase()
        return rx


class Manager:
    def __init__(self):
        self.dev, self.mode, self.profile, self.identity = None, 'disconnected', 'stock', ''
        self.downloads = {}
        self.input_port, self.output_port = '', ''
        self.candidate = None

    def interrupt(self):
        for device in (self.dev, self.candidate):
            if device is not None and hasattr(device, 'interrupt'):
                device.interrupt()

    def disconnect(self):
        device = self.dev
        self.dev, self.mode, self.profile, self.identity = None, 'disconnected', 'stock', ''
        self.input_port, self.output_port = '', ''
        if device is not None:
            device.close()

    def connection_failed(self):
        if self.dev is None:
            return False
        interrupted = getattr(self.dev, 'interrupted', None)
        return getattr(self.dev, 'failed', False) or (interrupted is not None and interrupted.is_set())

    def status(self):
        return {'connected': self.dev is not None, 'mode': self.mode, 'profile': self.profile,
                'identity': self.identity, 'input': self.input_port, 'output': self.output_port}

    def run(self, action, args, log=print, cancelled=lambda: False):
        if action == 'connect':
            if self.dev:
                raise ValueError('Disconnect the current device first.')
            candidate = self.candidate = MidiDevice(args['input'], args['output'])
            try:
                candidate.open()
                if cancelled():
                    raise Cancelled('Connection cancelled.')
                rx = candidate.call(1)
                if not rx or len(rx) < 7:
                    raise RuntimeError('The device did not respond.')
                name, _ = S.cstr(rx, 7 + rx[6])
                if 'syntakt' not in name.lower():
                    raise ValueError(f'Unsupported device.')
                version = candidate.call(2)
                ver = version[5:].rstrip(b'\0').decode('cp1252', 'replace') if version else '?'
                self.dev, self.mode = candidate, 'device'
                self.input_port, self.output_port = args.get('input', ''), args.get('output', '')
                requested_profile = args.get('profile', 'auto')
                self.profile = 'stock' if requested_profile == 'auto' else requested_profile
                if self.profile not in ('stock', 'v4', 'v6', 'v7'):
                    raise ValueError('Unknown device profile.')
                self.identity = f'Device · {ver}'
                if requested_profile == 'auto':
                    self.profile = self.detect_profile(log)
                log('Connected: ' + self.identity)
                return self.status()
            except BaseException:
                candidate.close()
                self.disconnect()
                raise
            finally:
                self.candidate = None
        if action == 'disconnect':
            self.disconnect()
            return self.status()
        if not self.dev:
            raise RuntimeError('Connect your device first.')
        dev = Transport(self.dev, log, cancelled, args.get('bytes', 0))
        if action == 'download_group':
            items = args.get('items', [])
            if not 1 <= len(items) <= 256:
                raise ValueError('Select between 1 and 256 files.')
            archive, used, records = io.BytesIO(), 0, []
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
                for i, item in enumerate(items):
                    if cancelled():
                        raise Cancelled('Download cancelled.')
                    op = 'hz_get' if item.get('source') == 'high' else 'get'
                    result = self.run(op, item, log, cancelled)
                    data, filename = self.downloads.pop(result['download'])
                    used += len(data)
                    if used > 512 * 1024 * 1024:
                        raise ValueError('The selection exceeds 512 MB. Select fewer files.')
                    filename = f'{i + 1:03d}-' + filename
                    z.writestr(filename, data)
                    records.append({**item, 'file': filename})
                    log(f'Download {i + 1}/{len(items)}: {filename}')
                z.writestr('collection.json', json.dumps(records, ensure_ascii=False, indent=2))
            return self.download(archive.getvalue(), 'Synfact-backup.zip')
        if action == 'sample_list':
            stock = self.run('list', {'path': '/samples'}, log, cancelled)['entries']
            entries = []
            for e in stock:
                if e.get('valid') and not e.get('directory') and 1 <= e.get('slot', 0) <= 64:
                    entries.append({**e, 'source': 'stock', 'key': 'stock:' + str(e['slot']),
                                    'bytes': e.get('size', 0), 'frames': None})
            result = {'entries': entries, 'stock_used': len(entries), 'stock_total': 64,
                      'high_available': self.profile in ('v6', 'v7'), 'high_error': None,
                      'free': None, 'used': None, 'total': None, 'sequence': None}
            if result['high_available']:
                try:
                    high = self.run('hz_list', {}, log, cancelled)
                    result.update({k: high[k] for k in ('free', 'used', 'total', 'sequence', 'largest', 'copies')})
                    entries.extend({**e, 'source': 'high', 'key': 'high:' + str(e['id'])} for e in high['entries'])
                except Cancelled:
                    raise
                except Exception as error:
                    result['high_error'] = str(error)
            return result
        if action == 'sample_put':
            data = base64.b64decode(args['data'], validate=True)
            name = args.get('name', 'SAMPLE')
            with tempfile.TemporaryDirectory() as td:
                wav = Path(td) / 'sample.wav'
                wav.write_bytes(data)
                rate, mono = H.read_wav(wav)
                if rate > H.RATE:
                    raise ValueError('Convert the WAV to 48 kHz before importing.')
                frames = len(H.resample(mono, rate))
            stock = self.run('list', {'path': '/samples'}, log, cancelled)['entries']
            used = {e['slot'] for e in stock if e.get('valid')}
            free_slot = next((i for i in range(1, 65) if i not in used), None)
            try:
                stock_name = 0 < len(name.encode('cp1252')) <= 16
            except UnicodeEncodeError:
                stock_name = False
            if free_slot and frames <= 5 * H.RATE and stock_name:
                log(f'Automatic destination: stock sample slot {free_slot}')
                self.run('put', {**args, 'path': f'/samples/{free_slot}'}, log, cancelled)
                return {'source': 'stock', 'slot': free_slot, 'frames': frames}
            if self.profile not in ('v6', 'v7'):
                raise ValueError('This sample needs extended storage (over 5 seconds, a long name or full stock slots). Install a compatible loader.')
            log('Automatic destination: extended sample storage.')
            return {**self.run('hz_put', args, log, cancelled), 'source': 'high'}
        if action == 'sample_rename':
            if args.get('source') == 'stock':
                return self.run('rename', args, log, cancelled)
            if args.get('source') != 'high' or self.profile not in ('v6', 'v7'):
                raise ValueError('Invalid sample or device profile.')
            name = args['name']
            if not name or len(name.encode('utf-8')) > 32 or chr(0) in name:
                raise ValueError('The name must contain 1 to 32 UTF-8 bytes.')
            store = H.Store(dev, S, block=READ_BLOCK, rest=READ_REST, log=log)
            index, copies = store.current()
            if any(e['name'] == name and e['id'] != int(args['id']) for e in index.entries):
                raise ValueError('A sample with this name already exists.')
            index.find(str(args['id']))['name'] = name
            if H.problems(index):
                raise ValueError('Inconsistent sample index.')
            store.commit(index, copies)
            return {'ok': True}
        if action.startswith('hz_'):
            if self.profile not in ('v6', 'v7'):
                raise ValueError('Extended sample storage requires a compatible loader.')
            store = H.Store(dev, S, block=READ_BLOCK, rest=READ_REST, write_block=8192, write_rest=0, log=log)
            if action == 'hz_init':
                H.cmd_init(store)
            elif action in ('hz_compact', 'hz_restore'):
                if action == 'hz_restore':
                    return compaction.restore(store, args['folder'], dev.report, cancelled)
                return compaction.compact(store, dev.report, cancelled)
            elif action == 'hz_list':
                index, copies = store.current()
                issues = H.problems(index)
                if issues:
                    raise ValueError('Inconsistent index: ' + '; '.join(issues))
                total = (index.windows - H.DATA_FIRST_K) * H.WINDOW
                free = H.free_blocks(index, index.windows) * H.WINDOW
                return {'entries': index.entries, 'sequence': index.seq, 'free': free,
                        'total': total, 'used': total - free,
                        'largest': H.largest_hole(index, index.windows) * H.WINDOW,
                        'copies': [c.seq if c else None for c in copies]}
            elif action == 'hz_put':
                index, copies = store.current()
                if not 1 <= index.next_id <= 0x7fff:
                    raise ValueError('High memory sample IDs are exhausted (maximum 32767). '
                                     'Deleting or compacting samples does not reset IDs.')
                data = base64.b64decode(args['data'], validate=True)
                with tempfile.TemporaryDirectory() as td:
                    wav = Path(td) / 'sample.wav'
                    wav.write_bytes(data)
                    rate, _ = H.read_wav(wav)
                    if rate > H.RATE:
                        raise ValueError('Convert the WAV to 48 kHz before importing to avoid aliasing.')
                    return H.cmd_put(store, wav, args.get('name'), state=(index, copies))
            elif action == 'hz_get':
                with tempfile.TemporaryDirectory() as td:
                    out = Path(td) / 'sample.wav'
                    H.cmd_get(store, str(args['id']), out)
                    return self.download(out.read_bytes(), args.get('name', 'sample') + '.wav')
            elif action == 'hz_rm':
                H.cmd_rm(store, str(args['id']))
            elif action == 'hz_check':
                return {'issues': H.cmd_fsck(store, bool(args.get('deep')))}
            else:
                raise ValueError('Unknown sample action.')
            return {'ok': True}
        if action == 'list':
            path = directory(args['path'])
            result = S.datalist(dev, path)
            if result is None:
                raise RuntimeError('The device could not list this folder.')
            entries = result[1]
            for e in entries:
                e['directory'] = S.is_dir(e)
                e['path'] = path.rstrip('/') + '/' + (e['name'] if e['directory'] else str(e['slot']))
            return {'entries': entries, 'path': path}
        if action == 'get':
            path = device_path(args['path'])
            obj = read_object(dev, path)
            name = args.get('name', 'backup')
            if path.startswith('/samples/'):
                sample = S.parse_sample(obj)
                if sample['smagic'] != 0x53414D50 or not sample['crc_ok']:
                    raise ValueError('Invalid sample.')
                with tempfile.TemporaryDirectory() as td:
                    out = Path(td) / 'sample.wav'
                    H.write_wav(out, sample['payload'])
                    return self.download(out.read_bytes(), name + '.wav')
            kind, ext = ('Project', '.stprj') if path.startswith('/projects/') else ('Sound', '.stsnd')
            return self.download(package(obj, name, kind), name + ext)
        if action == 'put':
            path = device_path(args['path'])
            data = base64.b64decode(args['data'], validate=True)
            if path.startswith('/samples/'):
                with tempfile.TemporaryDirectory() as td:
                    wav = Path(td) / 'sample.wav'
                    wav.write_bytes(data)
                    rate, mono = H.read_wav(wav)
                    if rate > H.RATE:
                        raise ValueError('Convert the WAV to 48 kHz before importing.')
                    pcm = H.to_pcm16be(H.resample(mono, rate))
                    if len(pcm) > S.MAX_SAMPLE_BYTES:
                        raise ValueError('Stock samples support up to 5 seconds. Use extended storage for longer samples.')
                    parts = S.build_sample(pcm, int(path.rsplit('/', 1)[1]), args.get('name', 'SAMPLE'))
            else:
                manifest, obj = read_package(data, allow_digitone=path.startswith('/soundbanks/'),
                                             filename=args.get('filename', ''),
                                             index=int(path.rsplit('/', 1)[1]) - 1, log=log)
                expected = 'Project' if path.startswith('/projects/') else 'Sound'
                if not manifest or manifest.get('FileType') != expected:
                    raise ValueError('The file type does not match the destination.')
                parts = [obj[:-12], obj[-12:]]
            if not S.data_write(dev, path, parts, block=8192, rest=WRITE_REST):
                raise RuntimeError('The device did not confirm the written size.')
            sent = validate_object(b''.join(parts))
            back = read_object(dev, path)
            # Device may regenerate slot/header fields; compare actual record data.
            if E.parse_object(back)['record'] != E.parse_object(sent)['record']:
                raise RuntimeError('Verification does not match the uploaded data.')
            log('Upload complete and verified.')
            return {'ok': True}
        if action in ('clear', 'copy', 'move', 'swap', 'rename'):
            src = device_path(args['path'])
            flags = {'clear': 8, 'copy': 16, 'move': 64, 'swap': 32, 'rename': 4}
            listed = S.datalist(dev, src.rsplit('/', 1)[0])
            entry = next((e for e in (listed[1] if listed else []) if e.get('slot') == int(src.rsplit('/', 1)[1])), None)
            if not entry or not entry.get('ops', 0) & flags[action]:
                raise ValueError('The device does not support this operation in the selected slot.')
            payload = src.encode('cp1252') + b'\0'
            if action == 'rename':
                label = args['name'].encode('cp1252')
                if not label or len(label) > 16 or b'\0' in label:
                    raise ValueError('Name: 1 to 16 Windows-1252 bytes.')
                payload += label + b'\0'
            elif action != 'clear':
                dst = device_path(args['destination'])
                if src.rsplit('/', 1)[0] != dst.rsplit('/', 1)[0]:
                    raise ValueError('The destination must be in the same folder.')
                if src == dst:
                    raise ValueError('Source and destination must be different.')
                payload += dst.encode('cp1252') + b'\0'
            typ = {'clear': 0x5C, 'copy': 0x5B, 'move': 0x5A, 'swap': 0x5D, 'rename': 0x5E}[action]
            S._ok(dev.call(typ, payload), action)
            return {'ok': True}
        if action.startswith('machine_'):
            if action != 'machine_put' and self.profile == 'stock':
                raise ValueError('Mods files require mods loader V4 or later.')
            path = '/samples/449'
            if action == 'machine_get':
                obj = read_object(dev, path)
                parsed = F.parse_object(obj)
                if not parsed['crc_ok'] or not parsed['footer_ok']:
                    raise ValueError('Mods file CRC mismatch.')
                info = machine_info(parsed['data'])
                return {**info, **self.download(parsed['data'], 'mods.smz4')}
            if action == 'machine_put':
                data = base64.b64decode(args['data'], validate=True)
                info = machine_info(data)
                parts = F.build_object(449, data, args.get('name', 'MODS'))
                self.prepare_mods_storage(dev, log)
                if not S.data_write(dev, path, parts, block=8192, rest=WRITE_REST):
                    raise RuntimeError('Write not confirmed.')
                parsed = F.parse_object(read_object(dev, path))
                if not parsed['crc_ok'] or not parsed['footer_ok'] or parsed['data'] != data:
                    raise RuntimeError('Mods file verification failed.')
                self.profile = 'v7' if info['format'] == 'SAFE' or info['sites'] == 77 else 'v6'
                log('Mods file uploaded and verified. Restart your device to load it.')
                return info
            raise ValueError('Unknown mods action.')
        if action == 'firmware_put':
            data = base64.b64decode(args['data'], validate=True)
            split_sysex(data)
            rx = dev.call(0x50, struct.pack('<I', len(data)) + b'sysex' + bytes((0, 1)), timeout=10)
            if rx is None or len(rx) < 6 or rx[5] != 0:
                raise RuntimeError('The device did not accept the firmware.')
            for offset in range(0, len(data), 2048):
                chunk = data[offset:offset + 2048]
                payload = S.be32(S.ecrc(chunk)) + S.be32(len(chunk)) + S.be32(offset) + chunk
                rx = dev.call(0x51, payload, timeout=15)
                if rx is None or len(rx) < 10 or rx[9] > 1:
                    raise RuntimeError('Firmware transfer failed.')
                if rx[9] == 1 and offset + len(chunk) < len(data):
                    raise RuntimeError('The device ended the transfer early.')
                dev.report(min(offset + 2048, len(data)), len(data), 'Uploading firmware')
                time.sleep(0.05)
            return {'ok': True, 'bytes': len(data), 'installed': None}
        if action == 'sysex_send':
            data = base64.b64decode(args['data'], validate=True)
            frames = split_sysex(data)
            for i, frame in enumerate(frames):
                if cancelled():
                    raise Cancelled('SysEx transfer cancelled. Check the device status.')
                self.dev.send_message(list(frame))
                time.sleep(0.08)
                if i % 16 == 0:
                    log(f'SysEx: {i + 1}/{len(frames)} messages sent')
            log('Messages sent. Check the result and version on your device; installation is not confirmed.')
            return {'frames': len(frames), 'installed': None}
        raise ValueError('Unknown action.')

    def prepare_mods_storage(self, dev, log):
        # Probe the firmware through raw reads instead of the cached profile:
        # first installations have neither mods nor an HZFS index to detect.
        log('Checking extended sample storage before uploading mods...')
        store = H.Store(dev, S, block=READ_BLOCK, rest=READ_REST, write_block=8192, write_rest=0, log=log)
        copies, damaged = [], False
        for k in H.INDEX_K:
            if dev.cancelled():
                raise Cancelled('Transfer cancelled.')
            try:
                raw, size = store.read_index_raw(k, 'Checking sample storage')
            except Exception as error:
                if dev.cancelled():
                    raise Cancelled('Transfer cancelled.') from error
                raise RuntimeError('Cannot read extended storage. Check the MIDI connection and make sure the Synfact firmware is running after restart.') from error
            if size != H.WINDOW:
                raise RuntimeError('Extended storage is unavailable. Install the Synfact firmware, restart the device, and reconnect before uploading mods.')
            try:
                index = H.Index.unpack(raw)
            except (ValueError, struct.error):
                index = None
            copies.append(index)
            damaged |= raw[:4] == H.MAGIC and index is None
        if any(copies):
            log('Existing extended sample storage preserved.')
            return
        if damaged:
            raise RuntimeError('HZFS indexes are damaged or incompatible. Automatic initialization stopped to preserve existing samples.')
        log('Initializing extended sample storage for first use...')
        try:
            H.cmd_init(store)
        except Exception as error:
            if dev.cancelled():
                raise Cancelled('Transfer cancelled.') from error
            raise

    def detect_profile(self, log):
        profile = 'stock'
        try:
            obj = read_object(self.dev, '/samples/449', log=log)
            parsed = F.parse_object(obj)
            if parsed['crc_ok'] and parsed['footer_ok']:
                info = machine_info(parsed['data'])
                profile = 'v7' if info['format'] == 'SAFE' or info['sites'] == 77 else 'v4'
        except ConnectionLost:
            raise
        except Exception:
            pass
        try:
            store = H.Store(self.dev, S, block=READ_BLOCK, rest=READ_REST, log=log)
            store.current()
            if profile != 'v7':
                profile = 'v6'
        except ConnectionLost:
            raise
        except Exception:
            pass
        return profile

    def download(self, data, name):
        import uuid
        key = uuid.uuid4().hex
        safe = re.sub(r'[^\w. -]', '_', name)[:100] or 'download.bin'
        self.downloads[key] = (data, safe)
        while len(self.downloads) > 10:
            self.downloads.pop(next(iter(self.downloads)))
        return {'download': key, 'filename': safe, 'bytes': len(data)}


def split_sysex(data):
    frames, p = [], 0
    while p < len(data):
        if data[p] != 0xF0:
            raise ValueError('The file must contain only F0…F7 SysEx messages.')
        end = data.find(b'\xF7', p + 1)
        if end < 0 or any(v > 127 for v in data[p + 1:end]):
            raise ValueError('Incomplete or invalid SysEx message.')
        frame = data[p:end + 1]
        if not frame.startswith(b'\xf0\x00\x20\x3c'):
            raise ValueError('The file contains unsupported SysEx messages.')
        frames.append(frame)
        p = end + 1
    if not frames:
        raise ValueError('Empty SysEx file.')
    return frames
