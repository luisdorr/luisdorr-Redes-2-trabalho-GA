"""Utility helpers to manipulate kernel routes from OSPF-Gaming."""

from __future__ import annotations

import logging
import subprocess
from typing import List, Optional

_LOGGER = logging.getLogger(__name__)


def _run_ip_command(arguments: List[str]) -> None:
    """Execute an ``ip route`` command and log potential errors."""

    cmd = ["ip", "route", *arguments]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        _LOGGER.error(
            "Command '%s' failed with exit code %s: %s", " ".join(cmd), exc.returncode, exc.stderr
        )
        raise
    except OSError as exc:  # pragma: no cover - defensive guard
        _LOGGER.error("Unable to execute '%s': %s", " ".join(cmd), exc)
        raise


def add_route(destination_prefix: str, next_hop_ip: str, interface: Optional[str] = None) -> None:
    """Install a unicast route towards ``destination_prefix`` via ``next_hop_ip``."""

    arguments = ["add", destination_prefix, "via", next_hop_ip]
    if interface:
        arguments.extend(["dev", interface])
    _run_ip_command(arguments)


def delete_route(destination_prefix: str) -> None:
    """Remove an existing unicast route for ``destination_prefix`` if present."""

    _run_ip_command(["del", destination_prefix])


__all__ = ["add_route", "delete_route"]
