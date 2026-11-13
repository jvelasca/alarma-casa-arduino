"""Enlaces con el dispositivo: real y simulado."""

from .base import Link, LinkError
from .serial_link import SerialLink, describe_ports, list_candidates
from .simulated_link import SimulatedLink

__all__ = [
    "Link",
    "LinkError",
    "SerialLink",
    "SimulatedLink",
    "describe_ports",
    "list_candidates",
]
