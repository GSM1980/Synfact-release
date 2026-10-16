# Synfact Transfer 0.0.1

Synfact Transfer is a desktop application for managing local device transfers
over USB MIDI. It provides a desktop window, file selection dialogs and a modal
transfer progress bar.

**This repository currently provides source code and build scripts only. No
precompiled executables or application ZIPs are published.** Build the app for
your platform using the instructions below, or run it from source as described
in [Development and current status](#development-and-current-status).

The build scripts bundle Python, Qt and the interface engine into a standalone
application. Once that package has been built, running it does not require a
separate Python, Qt or browser installation.

## Features

- **Drop:** upload firmware (`.syx`) or mods (`.smz4`). Current SAFE envelopes
  and legacy mods headers are supported. The original file is transferred
  unchanged and verified by readback; the application does not build firmware.
- **Projects:** browse 128 slots and import/export `.stprj` files.
- **Sounds:** browse banks A–H and import/export `.stsnd` files.
- **Samples:** import WAV audio, browse stock and extended storage, download
  samples and delete entries. S1–S64 are stock slots; H entries are samples for
  slots 65–128, with an approximately 60-second playback limit in compatible mods.
- Automatic device discovery, reconnect controls and queued transfers.
- English interface and a **Support this project** button opening Ko-fi in the
  default browser.

The app is for compatible devices and mods; it is not a generic MIDI librarian.
The device firmware must support the feature being used. Extended storage and
mods uploads require a compatible loader. Firmware uploads acknowledge transfer
reception; they do not certify that installation has finished.

WAV import supports PCM 8/16/24/32-bit and float32, mono mixing and conversion
to PCM16 at 48 kHz. Input above 48 kHz is rejected. Stock slots support samples
up to 5 seconds; longer samples require extended storage. Extended sample IDs
identify stored files, rather than sound banks. Deleting samples can leave
references in existing projects.

## Run a locally packaged build

This section applies after you have built the app using the instructions below.
The ZIPs and applications mentioned here are local build outputs, not published
downloads.

**Windows:** extract the complete portable ZIP and run
`SynfactTransfer/SynfactTransfer.exe`. Keep `_internal` beside the executable.
An optional single-file build provides `SynfactTransfer.exe` on its own; this
extracts its bundled runtime to a temporary directory at startup.

**macOS:** extract the ZIP and copy `SynfactTransfer.app` to Applications.
The ZIP contains a complete app bundle; do not copy only its executable.

Connect the device over USB MIDI and close other applications using its MIDI
ports. Device discovery is automatic. Uploads require confirmation. Closing
the app cancels pending jobs and waits for the active device request to return.
There is no demo mode, rename action or sample pool editor in this version.

## Build requirements

Only the build machine needs these tools:

- Git, to clone this repository.
- Python **3.12**, matching the target CPU architecture, with `pip` and `venv`.
- Internet access to install the packages in `requirements-build.txt`.

Python, PySide6/Qt and python-rtmidi are bundled in the resulting application.
PyInstaller is used for packaging. This is a packaged Python application, rather
than a C/C++ rewrite without libraries.

Build each target on its corresponding operating system. A Windows build cannot
produce a macOS app. Apple Silicon and Intel builds are separate; the scripts do
not produce a universal2 app. The pinned Qt 6.8 runtime targets Windows 10/11 x64
and macOS 12 or later; validate the completed app on the intended machine.

Official references: [Python downloads](https://www.python.org/downloads/),
[Qt 6.8 supported platforms](https://doc.qt.io/qt-6.8/supported-platforms.html),
[PyInstaller usage](https://pyinstaller.org/en/stable/usage.html).

## Get the source

```sh
git clone https://github.com/GSM1980/Synfact-release.git
cd Synfact-release/synfact-transfer
```

Run all commands below from this `synfact-transfer` directory.

## Windows x64

Install Python 3.12 for x64, including the Python launcher (`py`). In Command
Prompt, check the interpreter architecture:

```bat
py -3.12 -c "import platform; print(platform.machine())"
```

It should report `AMD64`. Then build:

```bat
build-windows.cmd
```

The script creates `.venv-build`, installs the pinned build requirements and
generates a portable app directory plus a distribution ZIP.

To generate a single executable instead:

```bat
build-windows.cmd --onefile
```

For a quick development build without ZIP packaging:

```bat
build-windows.cmd --quick
```

If the launcher is unavailable, use a Python 3.12 x64 interpreter directly:

```bat
python -m venv .venv-build
.venv-build\Scripts\python.exe -m pip install -r requirements-build.txt
.venv-build\Scripts\python.exe build.py
```

## macOS — Apple Silicon (M1 and later)

Use native **arm64 Python 3.12** in a native Terminal session. Check its
architecture before creating the build environment:

```sh
python3.12 -c "import platform; print(platform.machine())"
```

It must report `arm64`. Then run:

```sh
sh build-macos.sh
```

This creates an arm64 app bundle and ZIP. If the interpreter has a different
path, specify it explicitly:

```sh
PYTHON=/absolute/path/to/arm64/python3.12 sh build-macos.sh
```

## macOS — Intel

On an Intel Mac, use **x86_64 Python 3.12**. Check:

```sh
python3.12 -c "import platform; print(platform.machine())"
```

It must report `x86_64`. Then run:

```sh
sh build-macos.sh
```

This creates an x86_64 app bundle and ZIP. You can also select its interpreter:

```sh
PYTHON=/absolute/path/to/x86_64/python3.12 sh build-macos.sh
```

Use separate source checkouts or separate build environments for Intel and
Apple Silicon. The script reuses `.venv-build` if it already exists; changing
`PYTHON` does not change an existing environment's architecture.

For either Mac architecture, the equivalent manual commands are:

```sh
python3.12 -m venv .venv-build
.venv-build/bin/python -m pip install -r requirements-build.txt
.venv-build/bin/python build.py
```

If pip needs to build a dependency from source, install Apple's Command Line
Tools with `xcode-select --install` and retry. No separate Qt SDK is needed
when the pinned binary wheels are available.

## Build outputs

| Build | Application | Distribution |
| --- | --- | --- |
| Windows portable | `dist/SynfactTransfer/SynfactTransfer.exe` | `releases/SynfactTransfer-0.0.1-Windows-AMD64.zip` |
| Windows single file | `dist/SynfactTransfer.exe` | `releases/SynfactTransfer-0.0.1-Windows-AMD64-single.zip` |
| Mac Apple Silicon | `dist/SynfactTransfer.app` | `releases/SynfactTransfer-0.0.1-macOS-arm64.zip` |
| Mac Intel | `dist/SynfactTransfer.app` | `releases/SynfactTransfer-0.0.1-macOS-x86_64.zip` |

Close the running app before rebuilding into the same directory, especially on
Windows, where loaded Qt libraries cannot be replaced. Temporary build files
are written to `work/`. Full builds collect dependency notices in
`licenses/runtime/`; these generated files, environments, binaries and archives
are excluded from Git.

Local builds are unsigned. Public macOS distribution requires a separate
Developer ID signing and notarization process; the scripts do not provide it.

## Development and current status

Install `requirements.txt` in a virtual environment, then launch the source app:

```sh
python desktop.py
```

`desktop.py` owns the window and application lifecycle. `app.py` provides a
loopback-only API with a random session token; `core.py` performs serialized
device operations. `web/` contains the interface, `assets/` its icon, and
`vendor/` the existing device format and transport helpers.

Windows builds have been used for manual UI iterations. Automated tests have
intentionally not been rerun during these iterations. Old test and validation
artifacts are not included in this source snapshot. macOS build scripts are
provided, but current macOS binaries have not been compiled or validated here.

## License and support

Synfact Transfer and its bundled project device helper modules are licensed
under the [MIT License](LICENSE). Third-party dependency notices are retained
in `THIRD_PARTY_NOTICES.txt` and `licenses/DESKTOP-NOTICES.md`.
`PROVENANCE.json` records the origin and hashes of the vendor modules.
Bundled runtime components retain their own licenses.

If you would like to help the project grow,
[support this project on Ko-fi](https://ko-fi.com/ggsm80).
