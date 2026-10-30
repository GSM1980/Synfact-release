# Native browser port: in progress

The browser code uses JavaScript and Web MIDI SysEx. It does not load the
desktop engine, Python, or Pyodide. The desktop application remains separate.
The current preview supports connecting and listing projects, sounds, and stock
samples, and experimental project, sound and stock-sample transfers, including
Digitone conversion. Stock sample uploads accept WAV files up to five seconds
at rates up to 48 kHz. Extended samples, backup and other modifications are not
ported yet. Do not treat this preview as a replacement for desktop Transfer.
No physical-device validation has been performed.

Project and sound slots also offer rename, delete, copy, move and swap where
the instrument advertises support. Destination selection stays within the same
directory or bank. If the device does not acknowledge a change, refresh and
inspect the slots before retrying; the result may have been applied already.

Drop accepts mods (`.smz4`) and firmware (`.syx`). Mods are validated, sent to
`/mods`, then read back and compared byte-for-byte; restart the instrument after
a verified transfer. Firmware uses the device's OS upgrade protocol. A completed
firmware transfer does not confirm installation: wait for the instrument to
finish and follow its prompts. If a transfer is interrupted, check the device
before retrying. Neither path has yet been validated on physical hardware in the
browser port.

Multiple selected projects or sounds can be downloaded as a ZIP of individual
Transfer files, with a `collection.json` index. The archive is produced only if
every selected item is read and validated successfully; cancellation or errors
discard the partial collection. The combined file limit is 512 MiB.

For a local preview from the repository root:

```powershell
python synfact-transfer/browser/serve.py
```

Open http://127.0.0.1:8766/ in desktop Chrome or Edge and select Connect. Python
here is only a local static file server; a hosted site can serve the same HTML,
CSS and JavaScript without Python on the visitor's computer. Close the server
with Ctrl+C. This local server is not a production hosting configuration.
