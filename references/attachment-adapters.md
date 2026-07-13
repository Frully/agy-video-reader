# Attachment adapter contract

The controller selects one platform transport without ever falling back to a
plain-text video path. In this document, **implemented** means that transport
code and deterministic tests exist. It does not mean that the transport has
been exercised on its target operating system or accepted by a real `agy`
upload.

## Implementation and verification status

| Platform selector | Attachment transport | Implementation evidence | End-to-end runtime status |
| --- | --- | --- | --- |
| `darwin` | macOS file-URL pasteboard adapter | Implemented. The transport has prior macOS native-clipboard and authenticated `agy` upload evidence. The adapter refactor itself has not been re-run as an authenticated upload. | The established macOS runtime remains the supported path. |
| `win32` | Windows `CF_HDROP` shell file-list adapter | Implemented this iteration. Only deterministic mock and pure-function checks are available; it has not run against a Windows clipboard or a real `agy` upload. | Blocked independently of the clipboard adapter: the controller still needs Windows ConPTY, interprocess locking, process-tree cleanup, and private-cache ACL/reparse-point validation. Do not describe the Windows controller as runnable or verified yet. |
| native `linux*` | Linux X11/Wayland URI-list adapter | Implemented this iteration. Only deterministic fake-CopyQ and pure-function checks are available; it has not run against an X11 or Wayland clipboard or a real `agy` upload. | The POSIX controller path is implemented but remains native-unverified on Linux. Treat it as experimental until target-OS clipboard, PTY, cleanup, and upload checks pass. |
| WSL | none | WSL is deliberately distinct from native Linux because its host clipboard, display session, and path translation have different semantics. | Not enabled; do not select either the native Windows or native Linux adapter implicitly. |
| other values | unsupported fail-closed sentinel | No implementation evidence. | Return `ATTACHMENT_ADAPTER_UNAVAILABLE` before touching input or runtime state. |

Deterministic coverage establishes serialization, envelope parsing,
script/command construction, POSIX backup-mode checks, size bounds, and pure
state-machine branches. It does not execute CopyQ JavaScript, validate Windows
ACLs or reparse-safe handles, or prove native compare-and-set behavior. It also
cannot establish any of the following:

- that the target desktop actually publishes the intended native clipboard
  representation;
- that `Ctrl+V` in the target `agy` TUI consumes exactly one file attachment;
- that `agy` confirms a clipboard-sourced `video/*` item and uploads it;
- that the native clipboard survives staging, restoration, races, interruption,
  desktop-session changes, and process termination; or
- that the target runtime's PTY, locking, cache, path-security, signal, and
  child-process behavior matches the macOS evidence.

Accordingly, never report Windows or Linux native support as tested merely
because their deterministic suite passes.

## Windows and CF_HDROP

The Windows bridge uses the Win32 clipboard API through Python `ctypes` and
stages one Unicode `DROPFILES` payload as `CF_HDROP`; it does not place a
textual path on the clipboard. Before mutation it materializes bounded opaque
`HGLOBAL` formats, records registered-format identities, writes a checksummed
JSON recovery artifact, and collapses Windows-enumerated synthesized conversion
groups to their first (stored) representation. If that representation is a
GDI/owner/private handle format that cannot be serialized with its semantics,
the bridge fails before `EmptyClipboard` rather than substituting a converted
payload. Failed partial staging is restored only when both the captured
sequence number and partial payload still match.

These decisions and the `CF_HDROP` byte layout have deterministic coverage,
but the `ctypes` ABI and native clipboard have not been exercised on Windows.
The recovery file also lacks Windows-specific DACL and reparse-safe handle
validation, so the full controller remains gated before it touches caller
inputs or runtime state.

## Linux and CopyQ

The Linux bridge depends on CopyQ and supports explicit X11 and Wayland
adapters. It publishes `text/uri-list` and
`x-special/gnome-copied-files` for the staged file; it never adds `text/plain`
or pastes the filesystem path as ordinary text. A usable Linux environment
must have the expected display session, the CopyQ command, and a reachable,
long-lived CopyQ server. The non-mutating preflight and every mutating bridge
script reject CopyQ configurations that synchronize CLIPBOARD and PRIMARY;
the adapter does not back up or modify PRIMARY deliberately.

This lifetime requirement is part of the transport contract. On Linux, a
clipboard selection is served by an owning process rather than made durable by
the one-shot command that submitted it. Each bridge operation is a short-lived
CopyQ client request, while the CopyQ server retains ownership and serves the
staged or restored MIME data. If that server is absent, becomes unreachable, or
exits during the transaction, the adapter must fail rather than claim that the
clipboard is staged or restored.

The deterministic Linux tests use a fake CopyQ boundary. They cover the bridge
protocol and data handling, not a real CopyQ server, compositor, X server,
Wayland session, clipboard manager, terminal, or `agy` process. Headless Linux
and WSL therefore do not inherit native-Linux availability.

The bridge preserves the MIME order reported to it and all bounded binary MIME
payloads except `application/x-copyq-owner`, which is CopyQ-generated control
metadata and is deliberately regenerated rather than falsely reported as
restored. Whether a real CopyQ server preserves that order and all custom data
must be checked separately on X11 and Wayland. CopyQ exposes comparison and
copy as separate clipboard operations, so the final compare-to-write interval
is not atomic and a concurrent clipboard update may still race it. The selected
client Qt backend also does not prove which native backend an already-running
CopyQ server owns. These are explicit residual risks; the Linux adapter is not
described as lossless or race-safe until native evidence exists.

## Small public interface

An adapter has three responsibilities:

1. Preflight its native helper and environment without probing a fallback
   transport.
2. Create one stateful transaction for a staged video and a private recovery
   artifact.
3. Stage the platform-native file representation, expose the exact PTY bytes
   needed to submit it, and restore or conservatively recover global clipboard
   state.

Native details stay inside the transaction:

- macOS owns Swift bridge commands, pasteboard `changeCount`, file-URL typing,
  and the choice between `restore` and `recover`;
- Windows owns `CF_HDROP` construction, normalization of Win32-synthesized
  conversion formats, Windows clipboard backup/restoration, and
  sequence-plus-payload external-change detection; and
- Linux owns X11/Wayland URI MIME data, CopyQ client/server coordination,
  multi-MIME backup/restoration, and fingerprint-based external-change
  detection.

The runner continues to own source validation and snapshotting, the global run
lock, terminal state recognition, authoritative `video/*` confirmation, prompt
submission, result validation, and cleanup ordering. Extracting the attachment
transport does not by itself make those runner responsibilities portable.

## Promotion criteria

Before promoting Windows or Linux from implemented-but-unverified to supported,
collect all of the following on the target operating system:

- proof that the native file-list or URI representation is present and is not
  a plain-text path;
- a real attachment run in which `agy` confirms exactly one clipboard-sourced
  `video/*` item;
- an authenticated real-video result using the fixed CLI version and model;
- clipboard restoration checks before and after attachment, including an
  external-change race and interrupted cleanup;
- environment-specific coverage (Windows desktop; Linux X11 and Wayland
  separately); and
- verified cache privacy, ownership-safe cleanup, locking, terminal control,
  signals, and descendant-process cleanup.

Windows additionally requires completion of the non-clipboard runtime work
listed in the status table before any end-to-end run is enabled. WSL requires a
separate design and evidence set rather than promotion through either native
platform row.
