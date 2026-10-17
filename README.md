# Synfact-release

A mod system for the Elektron Syntakt.

> **Not affiliated with, endorsed by, or supported by Elektron.** This is unofficial, experimental software. Modified firmware can make your device unusable, cause data loss, and may affect your warranty. You are responsible for any changes to your instrument. Use at your own risk. Read the [legal disclaimer](LEGAL.md) before building or installing.

## Features

- **Digitakt MK1 sample machines:** SP ONE SHOT, SP WERP, SP REPITCH, and SP SLICE.
- **Per project sample pool**
- **Long sample support in slots 65–128.** Existing projects are migrated with their 64 stock samples in sample pool slots 1–64, leaving slots 65–128 free for long samples. After migration, you can organize and replace samples as you prefer using the sample manager and sample pool.
- **Basic sample manager and sample pool** for organizing samples and assigning them to project slots.
- **Sampling and resampling capabilities.** Press the **SOUND button twice in quick succession** to open the recorder and capture external inputs or internal audio.

## Roadmap

Planned future modifications, not included in the current release:

- **Full stereo sampling**
- **FM TONE port from Digitone MK1.**
- **Polyphonic mode using voices from other tracks**, with a slave mode for each linked track.
- **Master compressor.**
- **Third effect send**, starting with chorus.
- **Third LFO.**

## Build the modified firmware

You need **Python 3.10 or later**, an internet connection, and the **official, unmodified Elektron Syntakt OS 1.41 file**. Obtain that file from Elektron; it is not included in this repository. No Python packages or compilers need to be installed.

From the repository root:

```sh
python build.py "/path/to/Syntakt_OS1.41.syx"
```

On macOS/Linux, you can use `python3`. On Windows, for example:

```powershell
python build.py "C:\Downloads\Syntakt_OS1.41.syx"
```

The output is saved in `dist/`:

- `synfact_syx_v0.0.1.syx`: modified firmware.
- `synfact_mods_v0.0.1.smz4`: the downloaded machines/mods file, without transformation or repackaging.
- `build-info.json`: the release version, URL, file sizes, and hashes used for the build.

To choose another output directory:

```sh
python build.py "/path/to/Syntakt_OS1.41.syx" --output "my-build"
```

The output directory must be new. The script validates both files before creating it and preserves the original. A download or integrity failure exits with code 1 without leaving a partial package. The script generates local files; it does not send MIDI or install firmware.

## Installation

Install the two generated files in this order:

1. **Flash the modified firmware first:** send `dist/synfact_syx_v0.0.1.syx` to the Syntakt using the **Drop** tab in [Synfact Transfer](synfact-transfer/readme.md), following the instrument's OS update procedure. Wait for the firmware installation to finish and follow any restart prompts on the instrument before proceeding. A completed file transfer alone does not mean the firmware installation has finished.
2. **Transfer the mods file afterwards:** once the modified firmware is running, use the **Drop** tab in Synfact Transfer to send `dist/synfact_mods_v0.0.1.smz4` to the Syntakt. Wait for the transfer and readback verification to complete. **Then restart the Syntakt to load the complete firmware.**

Both steps are required to install this release. If you selected a different build output directory, use the files from that directory instead of `dist/`.

## Pinned release and validation

This is the fixed **Synfact 0.0.1** release for **Syntakt OS 1.41**. It includes SP ONE SHOT, SP WERP, SP REPITCH, and SP SLICE.

`release/manifest.json` pins the SHA-256 hashes of the original, patch, generated firmware, and machines file. The input version is verified against the entire file, regardless of its name: other OS versions, truncated files, and already modified versions are rejected. The official OS 1.41 SHA-256 is:

```text
8e2488f462c4a5656396a895f113bcd415e9900fa8709340dccf45d4cb9ed19e
```

The patch is a zlib-compressed XOR mask. It is applied to the original bytes up to the pinned output length; this is not a source compilation or a modular system. The repository contains the mask and its metadata, without either complete SYX file. The output hash requires exact reproduction of the reference firmware, whose verification preserved sections 1, 2, 4, 5, 6, and 8, the bootloader/bootstrap/updater, and load destinations.

The machines file includes the Sample Manager bugfix for an empty extended sample library (V14r14); the firmware is unchanged. It is downloaded from this release's [Google Drive URL](https://drive.google.com/uc?export=download&id=1FErQPsjDNE0g4oQqLkg-Luhg7iK4H9hw). Its size and SHA-256 are checked: a login page, incomplete download, or replaced file is rejected. If the remote content changes, the build fails until a consistent release is prepared.

The reference release remains experimental: full boot/first-screen acceptance, projects with parameter locks, and complete hardware validation are pending. This build verifies reproduction and integrity; it does not extend that coverage or resolve the outstanding SYSTEM INFO meter issues.

## Tests and maintenance

Run the tests without Elektron firmware or internet access:

```sh
python -m unittest discover -s tests -v
```

Maintainers can reproduce the fixed patch and manifest from local reference artifacts:

```sh
python tools/make_release_patch.py --original "/path/to/official.syx" --modified "/path/to/synfact_syx_v0.0.1.syx" --machines "/path/to/synfact_mods_v0.0.1.smz4" --machines-url "https://drive.google.com/uc?export=download&id=1FErQPsjDNE0g4oQqLkg-Luhg7iK4H9hw" --output "release-copy"
```

The utility requires the official OS 1.41 and verifies the patch round-trip. Its output names and metadata are fixed to Synfact 0.0.1. It does not compile or certify new firmware.

Files matching `*.syx` and `*.smz4`, `dist/`, and temporary build directories are excluded from Git. Keep originals outside the repository.

## License

Project code and documentation are licensed under the [MIT License](LICENSE), using the [standard MIT license text](https://opensource.org/license/mit). This includes Synfact Transfer and the device helper modules shipped with it. Third-party dependencies retain their own licenses and notices; see [component notices](synfact-transfer/THIRD_PARTY_NOTICES.txt) and [desktop runtime notices](synfact-transfer/licenses/DESKTOP-NOTICES.md). The MIT license does not grant rights to Elektron firmware or other third-party materials; see the [legal disclaimer](LEGAL.md).

## Synfact Transfer

Standalone desktop transfer application, version 0.0.1.

[Source code, features and build instructions for Windows, Mac Intel and Apple Silicon](synfact-transfer/readme.md).

## Known issues

- **Sample manager:** Once installed, if no sample has been loaded into the +Drive's extended memory, the sample manager throws an exception. Until this bug is fixed, simply transfer a single sample using Synfact Transfer, or record/resample anything and save the result. As long as there is at least one sample in the +Drive's extended memory, the sample manager will start up fine.
