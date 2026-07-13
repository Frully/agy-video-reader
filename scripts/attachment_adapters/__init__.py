"""Platform selection for Antigravity attachment transports."""

from __future__ import annotations

import os
import platform as host_platform
from collections.abc import Mapping

from .base import (
    AttachmentAdapter,
    AttachmentFailure,
    AttachmentTransaction,
    UnsupportedAttachmentAdapter,
)
from .linux import (
    LinuxURIListClipboardAdapter,
    LinuxWaylandURIListClipboardAdapter,
    LinuxX11URIListClipboardAdapter,
    select_linux_backend,
)
from .macos import MacOSFileURLClipboardAdapter
from .windows import WindowsCFHDropClipboardAdapter


def create_attachment_adapter(
    *,
    platform_name: str,
    bridge_override: str | None = None,
    environment: Mapping[str, str] | None = None,
    platform_release: str | None = None,
) -> AttachmentAdapter:
    """Select one explicit adapter without falling back to a textual path."""

    if platform_name == "darwin":
        return MacOSFileURLClipboardAdapter(bridge_override)
    if platform_name == "win32":
        return WindowsCFHDropClipboardAdapter(bridge_override)
    if platform_name.startswith("linux"):
        env = os.environ if environment is None else environment
        release = host_platform.release() if platform_release is None else platform_release
        if (
            env.get("WSL_DISTRO_NAME")
            or env.get("WSL_INTEROP")
            or "microsoft" in release.casefold()
        ):
            return UnsupportedAttachmentAdapter(
                platform_name,
                message=(
                    "WSL clipboard transport is a separate environment and has not been implemented; "
                    "the native Linux adapters must not be selected for it."
                ),
            )
        backend = select_linux_backend(env)
        if backend == "x11":
            return LinuxX11URIListClipboardAdapter(bridge_override)
        if backend == "wayland":
            return LinuxWaylandURIListClipboardAdapter(bridge_override)
        return LinuxURIListClipboardAdapter(backend=None, bridge_override=bridge_override)
    return UnsupportedAttachmentAdapter(platform_name)


__all__ = [
    "AttachmentAdapter",
    "AttachmentFailure",
    "AttachmentTransaction",
    "LinuxURIListClipboardAdapter",
    "LinuxWaylandURIListClipboardAdapter",
    "LinuxX11URIListClipboardAdapter",
    "MacOSFileURLClipboardAdapter",
    "UnsupportedAttachmentAdapter",
    "WindowsCFHDropClipboardAdapter",
    "create_attachment_adapter",
]
