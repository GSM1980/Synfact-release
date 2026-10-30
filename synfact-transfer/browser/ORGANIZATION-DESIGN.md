# Project and sound organization: implementation proposal

Scope: projects and sound banks A–H, using the existing browser Web MIDI
connection. Extended samples and the desktop application are outside this change.

For each operation, re-read the source directory immediately before sending the
device command and require the source slot to be occupied and its `ops` bit to
permit the action. Reject paths outside the current category or bank.

| Operation | Device command | Required source bit | Confirmation |
| --- | --- | --- | --- |
| Rename | `0x5e` | `0x04` | Show old and new names. Accept 1–16 Windows-1252 bytes, excluding NUL. |
| Delete | `0x5c` | `0x08` | Show the exact source slot and name. |
| Copy | `0x5b` | `0x10` | Show source and destination; explicitly warn if the destination is occupied. |
| Move | `0x5a` | `0x40` | Show source and destination; explicitly warn if the destination is occupied. |
| Swap | `0x5d` | `0x20` | Show both source and destination slots and names. |

Copy, move and swap must reject the same source and destination and require the
destination to be in the same project directory or sound bank. The interface
must refresh after an acknowledged command; a timeout is reported as an
unknown outcome, not retried automatically. Cancellation or disconnection must
not replay queued changes. No device operation runs during implementation tests.

Tests should simulate the MIDI transport to check every command and encoded
argument, source capability flags, invalid and cross-bank paths, invalid names,
device rejection, timeout, and cancellation. UI tests should cover confirmation,
occupied destinations, and the refresh. Hardware testing should use disposable
slots and independently retained exports.
