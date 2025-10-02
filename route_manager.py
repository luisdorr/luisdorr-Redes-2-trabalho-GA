"""Route management utilities for OSPF-Gaming."""

import logging
import subprocess
from typing import Optional

_LOGGER = logging.getLogger(__name__)


def _run_ip_command(arguments: list[str]) -> None:
    """Executa o comando `ip route` com os argumentos fornecidos."""
    cmd = ["ip", "route"] + arguments
    _LOGGER.debug("Executando comando: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        _LOGGER.error("Erro ao executar comando '%s': %s", " ".join(cmd), e.stderr.strip())
        raise


def add_route(destination_prefix: str, next_hop_ip: str, interface: Optional[str] = None) -> None:
    """
    Adiciona ou substitui uma rota no kernel.
    
    Args:
        destination_prefix (str): Exemplo "10.0.1.0/24".
        next_hop_ip (str): IP do próximo salto.
        interface (Optional[str]): Interface de saída (ex.: "eth0").
    """
    arguments = ["replace", destination_prefix, "via", next_hop_ip]
    if interface:
        arguments.extend(["dev", interface])
    _run_ip_command(arguments)
    _LOGGER.info("Rota instalada: %s via %s%s",
                 destination_prefix, next_hop_ip,
                 f" dev {interface}" if interface else "")


def delete_route(destination_prefix: str) -> None:
    """
    Remove rota do kernel.
    
    Args:
        destination_prefix (str): Exemplo "10.0.1.0/24".
    """
    arguments = ["del", destination_prefix]
    try:
        _run_ip_command(arguments)
        _LOGGER.info("Rota removida: %s", destination_prefix)
    except Exception:
        _LOGGER.warning("Não foi possível remover rota %s (pode não existir).", destination_prefix)
