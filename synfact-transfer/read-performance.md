# USB MIDI read optimization

Read-only measurements on a connected Syntakt running OS 1.41, Windows,
Python 3.12 and the bundled python-rtmidi 1.5.8, on 2026-10-23.

## Selected settings

Normal downloads, upload verification and mods detection use 2 KiB blocks with
no fixed delay between read requests. Each request still waits for its reply.
The complete object's CRC is checked before accepting it. A truncated or corrupt
read closes its job and restarts once with 2 KiB blocks and 50 ms pauses.
Raw storage readers keep their own pacing and apply the same conservative
settings on their existing retries. Cancellation is propagated immediately.

Standard upload pacing stays at 50 ms; extended storage writes retain their
existing pacing. Firmware and raw SysEx upload delays are unchanged. Hardware
testing in this work authorized reads only.

## Measurements

Medians of three complete reads per setting in the retained comparison runs:

| Object size | Previous: 2 KiB / 50 ms | Selected: 2 KiB / 0 ms | Read speedup |
| --- | ---: | ---: | ---: |
| 91,759 bytes | 3.816 s | 1.519 s | 2.51x |
| 60,791 bytes | 2.569 s | 1.060 s | 2.42x |
| 524,331 bytes, raw storage object | 20.782 s | 7.331 s | 2.83x |

Every complete comparison read had the same SHA-256 for that object and a valid
object CRC. The first test series, under different scheduling conditions,
showed 3.21 s versus 0.80 s on the 91,759-byte object. Actual throughput depends
on the host and device workload; these measurements describe read phases,
not complete upload-plus-verification operations. Extended storage already
used zero delay, so it does not gain the improvement shown against 50 ms.

Larger 4, 8 and 16 KiB blocks were investigated. Although 8 KiB reached about
140 KiB/s in the initial series, subsequent 4 and 8 KiB runs had intermittent
truncated partials. The pinned RtMidi 1.5.8 has no `set_buffer_size` method;
the existing buffer-enlargement branch is therefore unavailable on this build.
The device also capped a 32 KiB read request to 16 KiB. Larger blocks were not
selected as the default. These observations do not isolate the source of the
packet loss to one component.

## Reproduce

Close applications holding the MIDI input, then run the benchmark with the
Transfer environment from the repository root:

```sh
python tools/benchmark_transfer_reads.py --path /samples/1 --blocks 2048 --rests 0.05 0 --repeat 3
```

Use an existing stock sample, sound or project. The benchmark also accepts the
mods read path `/mods` and raw read windows. Its transport permits only
DataReadOpen, DataReadPartial and DataReadClose. It prints timings, hashes and
errors as JSON lines, and returns a nonzero exit status for failed reads.
It never saves object contents. Local measurement files are kept under the
ignored `synfact-transfer/work/` directory.

The application reports average KiB/s separately for upload, download and
verification phases; its job log also records bytes and elapsed time.

## Sample library preparation

Valid HZFS indexes are now read through their declared used length, rather than
including the unused padding in their 512 KiB raw windows. The length is bounded
by the entry count and window size. Every used byte is covered by the index's
own CRC; this path deliberately does not read the raw object's padding/footer.
Missing or invalid indexes require a full-window read and outer-object CRC
validation before they can be classified as absent or damaged. Read failures
therefore cannot trigger automatic initialization.

On the connected Syntakt's existing library, both copies fit within the first
2 KiB data partial. The old preflight transferred 1,048,662 bytes in 33.47 s.
The optimized preflight transferred 4,096 bytes in 0.072 s; three independent
library reads took 0.073–0.077 s. Both copies matched the indexes obtained from
the full reads. This improvement applies to valid indexes; first-time or
damaged-library handling retains complete reads.

OS 1.41 responds to intentional early closes with `Reader did not complete`;
the prefix reader accepts that diagnostic after requesting the close. Other
close failures and invalid prefixes remain errors. Device tests used only
read requests, including the actual mods preflight with writes blocked.

Sample uploads reuse the checked index pair within the same operation, avoiding
a duplicate pair of reads. Index commits also verify only the used index bytes.
The UI now distinguishes **Checking sample library**, **Checking sample storage**,
**Updating sample library**, and **Verifying sample library** from actual file
upload/download and verification phases.

## 0.0.5: sample upload readback (2026-10-28)

Offline traces through the real upload path and the in-memory RPC simulator
found that the final short sample window was still being read in full. A
192-byte SAMP+PCM body caused 524,331 bytes of audio verification traffic.
The used catalog prefixes were already optimized in 0.0.3; ordinary uploads
do not reread the PCM of older samples. Catalog reads/writes still grow by
64 bytes per entry and are committed separately for every upload.

`Store.verify_blocks` now compares all uploaded bytes before publishing the
new entry. Full sample windows keep their complete object/footer CRC checks.
Only the last partial window uses the existing bounded raw prefix reader;
it validates the object identity/size, requests an explicit close and compares
the entire used prefix with the exact bytes sent. Unused old padding is outside
the sample. A prefix error or mismatch triggers a complete CRC-checked read;
a persistent mismatch leaves both catalog copies unchanged. Cancellation and
connection loss do not retry or commit. Downloads, compaction and deep checks
continue to use the complete-read path.

With 2 KiB read requests, that 192-byte body now needs one 2,048-byte partial.
Total receive payload during one upload (catalog A/B, new audio, catalog commit
readback), excluding protocol envelope bytes, is:

| Existing library entries | 0.0.4 | 0.0.5 |
| --- | ---: | ---: |
| 1 or 10 | 530,475 bytes | 8,192 bytes |
| 130 | 555,051 bytes | 32,768 bytes |
| 1,000 | 720,939 bytes | 198,656 bytes |

These are simulator byte counts, not hardware speed measurements. Corrupt or
unsupported prefix reads can still use the slower full-read fallback.

The verification UI reports logical used bytes for the sample-prefix scope.
Full reads derive their total from the returned object header (including
fragmented headers) instead of retaining a previous write's total. Conservative
fallback reads show an indeterminate **Verifying sample (full read)** phase.

Regression coverage includes small and multi-window samples, read boundaries,
transient/persistent corruption, missing/expected close responses, cancellation,
connection loss, unchanged catalog copies on failed verification and corrected
UI totals. Run `python -m unittest discover -s tests -p 'test_transfer*.py'`
and `node tests/test_transfer_progress_ui.js` from the repository root.
