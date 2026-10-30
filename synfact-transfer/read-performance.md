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
mods read path `/samples/449` and raw read windows. Its transport permits only
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
