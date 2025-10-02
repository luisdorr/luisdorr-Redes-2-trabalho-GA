from __future__ import annotations

import logging
import subprocess
from typing import Optional

_LOGGER = logging.getLogger(__name__)


def _run_ip(arguments: list[str]) -> None:
    cmd = ["ip", "route", *arguments]
    _LOGGER.debug("executing: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def add_route(prefix: str, next_hop: str, *, interface: Optional[str] = None) -> None:
    """Program a Layer-3 forwarding entry in the kernel."""

    arguments = ["replace", prefix, "via", next_hop]
    if interface:
        arguments.extend(["dev", interface])
    _run_ip(arguments)
    _LOGGER.info("route %s via %s%s", prefix, next_hop, f" dev {interface}" if interface else "")


def delete_route(prefix: str) -> None:
    """Withdraw a Layer-3 forwarding entry from the kernel."""

    try:
        _run_ip(["del", prefix])
    except subprocess.CalledProcessError as exc:
        _LOGGER.warning("failed to delete %s: %s", prefix, (exc.stderr or exc.stdout).strip())
    else:
        _LOGGER.info("removed route %s", prefix)


__all__ = ["add_route", "delete_route"]
