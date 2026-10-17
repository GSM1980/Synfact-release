"""Offline-backed HZFS rebuilds that preserve file IDs and project references."""
import os
from pathlib import Path
import shutil
import tempfile

import hzfs as H


def checked_body(body, entry):
    if (len(body) != entry['bytes'] or H.crc(body) != entry['crc']
            or body[:4] != b'SAMP' or H.u32(body, 8) != entry['frames']):
        raise ValueError(f"Sample {entry['id']} failed size/header/CRC verification.")
    return body


def durable_write(path, data):
    with path.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def load_backup(folder):
    """Validate the whole backup from disk before allowing any device writes."""
    index = H.Index.unpack((folder / 'index.hzfs').read_bytes())
    if index is None or index.windows <= H.DATA_FIRST_K or H.problems(index):
        raise ValueError('Invalid compaction backup index.')
    for entry in index.entries:
        checked_body((folder / f"{entry['id']}.samp").read_bytes(), entry)
    return index


def rebuild(store, index, folder, report, cancelled):
    """Rebuild from verified disk files; never allocate new sample IDs."""
    if store.capacity() != index.windows:
        raise ValueError('Backup and connected device capacities do not match.')
    copies = store.read_index_copies()
    sequence = max([index.seq] + [c.seq for c in copies if c])
    # Two empty commits plus one per file and a final mirror must not wrap.
    if sequence + len(index.entries) + 3 > 0xffffffff:
        raise ValueError('HZFS sequence exhausted; rebuild refused.')
    if cancelled():
        raise RuntimeError('Cancelled before rebuilding. Device unchanged.')
    current = H.Index(seq=sequence, windows=index.windows, next_id=index.next_id)
    # Include the backup sequence even if both device indexes are unreadable.
    copies = [H.Index(seq=sequence), None]
    total = sum(e['bytes'] for e in index.entries)
    done = 0
    first = H.DATA_FIRST_K
    try:
        report(0, total, 'Rebuilding high memory')
        store.commit(current, copies)
        store.commit(current, store.read_index_copies())
        for original in sorted(index.entries, key=lambda e: e['first']):
            if cancelled():
                raise RuntimeError('Rebuild cancelled.')
            body = checked_body((folder / f"{original['id']}.samp").read_bytes(), original)
            store.write_at(first, body)
            back = store.read_blocks(first, original['blocks'], original['bytes'])
            if back != body:
                raise RuntimeError(f"Sample {original['id']} did not read back identical.")
            current.entries.append({**original, 'first': first})
            present = {e['id'] for e in current.entries}
            current.slot_map = [eid if eid in present else 0 for eid in index.slot_map]
            store.commit(current, store.read_index_copies())
            first += original['blocks']
            done += original['bytes']
            report(done, total, 'Rebuilding high memory')
        # Both copies must describe the packed layout, releasing old reservations.
        store.commit(current, store.read_index_copies())
        copies = store.read_index_copies()
        for copy in copies:
            if (copy is None or H.problems(copy) or copy.entries != current.entries
                    or copy.slot_map != index.slot_map or copy.next_id != index.next_id):
                raise RuntimeError('Final HZFS index verification failed.')
        # Check all audio once more after every data and index write has finished.
        for entry in current.entries:
            checked_body(store.read_blocks(entry['first'], entry['blocks'], entry['bytes']), entry)
    except Exception as error:
        raise RuntimeError(f'{error} Recovery backup retained at: {folder}. '
                           'Use Restore backup in Samples > H on the same device.') from error
    return {'ok': True, 'count': len(current.entries),
            'free': H.free_blocks(current, current.windows) * H.WINDOW,
            'largest': H.largest_hole(current, current.windows) * H.WINDOW}


def compact(store, report, cancelled):
    index, _ = store.current()
    issues = H.problems(index)
    if issues or index.windows <= H.DATA_FIRST_K:
        raise ValueError('Inconsistent HZFS index; compaction refused: ' + '; '.join(issues))
    if store.capacity() != index.windows:
        raise ValueError('Index and connected device capacities do not match.')
    folder = Path(tempfile.mkdtemp(prefix='synfact-hzfs-'))
    try:
        durable_write(folder / 'index.hzfs', index.pack())
        total = sum(e['bytes'] for e in index.entries)
        done = 0
        report(0, total, 'Backing up high memory')
        for entry in index.entries:
            if cancelled():
                raise RuntimeError('Backup cancelled. Device unchanged.')
            body = checked_body(store.read_blocks(entry['first'], entry['blocks'], entry['bytes']), entry)
            durable_write(folder / f"{entry['id']}.samp", body)
            done += entry['bytes']
            report(done, total, 'Backing up high memory')
        saved = load_backup(folder)
    except Exception:
        # This directory was created here and no device writes have begun.
        shutil.rmtree(folder)
        raise
    try:
        latest, _ = store.current()
        if latest.pack() != saved.pack():
            raise RuntimeError('HZFS changed during backup. Device not rebuilt.')
        result = rebuild(store, saved, folder, report, cancelled)
    except Exception as error:
        if 'Recovery backup retained at:' in str(error):
            raise
        raise RuntimeError(f'{error} Recovery backup retained at: {folder}') from error
    try:
        shutil.rmtree(folder)
    except OSError:
        result['backup'] = str(folder)
    return result


def restore(store, path, report, cancelled):
    folder = Path(path).resolve()
    index = load_backup(folder)
    result = rebuild(store, index, folder, report, cancelled)
    # User-selected recovery copies are retained even after a successful restore.
    return {**result, 'backup': str(folder)}
