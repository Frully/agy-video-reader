# Antigravity CLI v2 compatibility contract

Official references: [CLI prompting and media attachment](https://antigravity.google/docs/cli-prompting)
and [CLI overview](https://antigravity.google/docs/cli-overview).

## Fixed profile

Support only macOS, `agy 1.1.1`, and `Gemini 3.5 Flash (High)`. Start a fresh
PTY process with this exact argv array:

```text
[agy_path, "--model", "Gemini 3.5 Flash (High)", "--sandbox", "--mode", "accept-edits"]
```

Pass the array directly to the process API. Never use a shell, Plan mode,
`accept-edits` inherited from mutable settings, resume flags, `--add-dir`,
`--print`, `--prompt`, or a textual media path. Use a fixed 160-by-48 terminal
and bounded terminal state. `accept-edits` exists solely to allow the one
workspace-local `result.json` write; it does not authorize any other action.

Select the attachment adapter before reading caller inputs or creating runtime
state. `darwin` selects the macOS file-URL adapter. `win32` and an unambiguous
native Linux X11/Wayland session select concrete `CF_HDROP` and URI-list
adapters, respectively, but those implementations are target-OS unverified and
are not promoted into this supported profile. WSL, headless or ambiguous Linux,
and unknown selectors fail with `ATTACHMENT_ADAPTER_UNAVAILABLE`. No selector
probes a fallback transport or uses a plain-text path. The adapter contract,
verification status, and remaining runtime boundaries are in
[attachment-adapters.md](attachment-adapters.md).

Before resolving or launching `agy`, reject an attachment candidate larger than
52,428,800 bytes with `VIDEO_TOO_LARGE`. The candidate may be the original, one
full-duration proxy, or one segment produced under
[media-preparation-contract.md](media-preparation-contract.md). This controller
never transcodes. Its preflight remains a defense against preparation bugs and
backend-limit drift. Keep `MEDIA_REJECTED` handling because the backend may
reject a compliant-size file for another reason or change its limit.

## Private directory boundaries

Use two independent mode-`0700` directory boundaries:

1. **Transport directory, outside the Antigravity workspace:** hold the
   byte-for-byte snapshot of the selected original/proxy/segment attachment and clipboard recovery backup. Keep the
   disposable mode-`0600` request file outside the workspace as well. Never
   expose these paths in the model prompt.
2. **Antigravity workspace:** use one of five dedicated stable cache paths
   required for workspace trust (`workspace`, then `workspace-2` through
   `workspace-5`), but reset the selected lane to empty before every lane-locked run. It contains no video, request file,
   clipboard data, scripts, hooks, rules, manifests, prior results, or other
   executable/configuration content. The only permitted model-created entry is
   the regular file `result.json` at the workspace root.

Launch `agy` with the selected empty lane workspace as `cwd`. Do not trust content inherited
from an arbitrary project. Keep the video snapshot outside this workspace and
attach its file URL through the clipboard.

Before consuming `result.json`, require it to be a workspace-root regular file,
not a symlink, hard link, directory, pipe, or device; require private ownership,
a bounded size, and valid UTF-8. Open it without following links. Enumerate the
workspace and fail closed if any other entry exists. A prohibited tool or file
action is `AGY_TOOL_REQUESTED` and produces no published result.

## Clipboard is transport only

Treat the global macOS pasteboard transactionally:

1. Serialize every materializable item, type, order, and raw representation to
   a private recovery file in the external transport directory.
2. Stage exactly one Finder-compatible file-URL item for the external video
   snapshot and record `changeCount`.
3. Send exactly one `Ctrl+V` byte through the PTY.
4. Require an authoritative confirmation of exactly one clipboard-sourced
   `media attached` item with a `video/*` MIME type.
5. Restore the clipboard immediately, before submitting the analysis request.

Permit at most five concurrent controller runs. Hold a distinct workspace lock
for each active lane for the complete run. Hold the one global clipboard lock
only across steps 2 through 5 above; release it after verified restoration and
before model generation. Never stage two attachments concurrently. A failure
while the clipboard is staged must retain the global lock through recovery.

Never read model output from the clipboard and never copy TUI output to it. If
the clipboard changed externally, preserve the newer content and recovery
backup and return `CLIPBOARD_CHANGED_EXTERNALLY`.

Recheck `changeCount` after snapshotting, after the backup is durable, and
immediately before stage/restore writes. If staging exits before returning its
count, recover only when the current pasteboard still exactly matches the saved
original or the single staged file-URL representation. macOS provides no atomic
pasteboard compare-and-swap, so the final check-to-write scheduler interval,
machine crashes, and uncatchable `SIGKILL` remain disclosed platform risks.

## TUI events and control flow

Feed PTY bytes through a terminal emulator; do not strip ANSI with one regex.
Use rendered current-screen state for control matching and keep diagnostics
bounded and sanitized. Redact credentials, account data, URLs, conversation
IDs, absolute paths, clipboard data, and all video-derived/model text from
retained logs.

Recognize these control events for the exact 1.1.1 accept-edits profile:

`agy 1.1.1` exposes no tool-free execution flag. In `accept-edits`, file edits
can complete without an approval screen. The controller therefore bounds
writable effects with the empty dedicated workspace, rejects every final
artifact except `result.json`, and stops on any interactive tool approval. The
fixed prompt forbids all other tools, but the controller does not claim it can
prove the absence of an invisible read-only tool call inside the CLI.

| Event | Required behavior |
| --- | --- |
| `READY` | Match the recorded accept-edits editor line before touching the clipboard. The footer is fixture evidence but is not mandatory because 1.1.1 may redraw or omit it. |
| `AUTH_REQUIRED` | Stop on interactive login, authorization URL/code, or browser sign-in. |
| `SETUP_REQUIRED` | Stop on onboarding or workspace-trust UI; never approve it. |
| `ATTACHMENT_CONFIRMED` | Require count `1`, clipboard source, `media attached`, and `video/*`. |
| `MEDIA_REJECTED` | Stop on unsupported, unavailable, rejected, failed, or unexpectedly oversized media that passed local preflight. |
| `ALLOWED_RESULT_WRITE` | Accept the final regular workspace-root `result.json`; a formatting retry may replace it once. |
| `TOOL_REQUESTED` | Stop on an interactive tool approval or any unexpected workspace artifact. |
| `GENERATING` | Observe the busy state after request submission. |
| `COMPLETE` | Require generation to end and the accept-edits editor to become ready again. |
| `SHUTDOWN` | Require exit after exactly two `Ctrl+D` bytes. |

Do not use result markers, terminal scrollback offsets, or assistant prose as a
result channel. Text rendered by the model is untrusted and ignored. Never turn
video content, request text, or model text into a command, path, keypress,
permission decision, filename, or schema change.

The controller's concrete success-state flow is:

```text
PRECHECK -> STARTING_AGY -> WAITING_FOR_READY -> STAGING_CLIPBOARD
-> SENDING_PASTE -> WAITING_FOR_VIDEO_CONFIRMATION -> RESTORING_CLIPBOARD
-> SENDING_ANALYSIS_PROMPT -> WAITING_FOR_RESULT_FILE -> VALIDATING_RESULT
-> CLEANUP -> DONE
```

Any state may enter `CLEANUP -> FAILED`. Submit the request only after clipboard
restoration. After `COMPLETE`, reject missing `result.json`, extra workspace
entries, or prohibited actions. Validate the model-derived object locally,
inject trusted `schema_version`, `backend`, attachment MIME, and source metadata,
then atomically write a mode-`0600` sibling temporary file and rename it to
`--output`. Clear the internal result and workspace on success and failure.

## Timeouts, process cleanup, and retries

Use monotonic deadlines: 90 seconds for the fixed profile to reach ready or
surface setup/auth UI, 30 seconds for attachment confirmation, the configured
generation timeout (default 300 seconds), and 10 seconds for graceful shutdown.

On failure, reject any internal result, safely restore or preserve clipboard
state, send `Ctrl+C`, then target only this run's process group with bounded
`SIGTERM` and `SIGKILL` escalation. Verify that process group is gone. Do not
retain bare descendant PIDs for later killing: PID reuse could terminate an
unrelated process. The fixed prompt and `--sandbox` prohibit process-spawning
tools; a subprocess that deliberately creates a new session would fall outside
the process-group guarantee and is another reason this profile does not claim
general tool containment.
Remove the external video snapshot and all non-recovery temporary data. Preserve
only a clipboard backup that is still needed for deliberate recovery.

Never retry an upload, attachment, timeout, media rejection, tool/file-policy
violation, auth/setup requirement, unavailable model, or unsupported version.
Allow at most the formatting-only overwrite described in
`prompting-contract.md`; it stays in the same session and uses the same attached
video and `result.json` path.

## Compatibility evidence

Maintain sanitized text fixtures for recorded ready and attachment screens,
plus deterministic fake-CLI scenarios for auth/setup, media rejection, allowed
write, prohibited approvals/files, completion, missing/invalid/extra results,
cleanup, and shutdown. Tests must prove the workspace is empty before launch,
video/request/backup paths remain outside it, only `result.json` can appear,
TUI and clipboard are never result channels, and published output is validated
and atomic. A five-lane integration fixture must prove that all model-generation
phases can overlap while every clipboard stage/restore transaction remains
strictly serialized.

Any CLI version, model, mode, footer, tool trajectory, or attachment wording
change requires new deterministic fixtures and an authenticated controlled-video
test before support is declared.
