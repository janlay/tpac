"""The controller identity that access points see."""
from __future__ import annotations

from dataclasses import dataclass

from .const import AC_FIRMWARE, AC_NAME


@dataclass
class AcIdentity:

    model: str = AC_NAME
    version: str = "7.0"
    firmware: str = AC_FIRMWARE

    @property
    def name(self) -> str:
        return self.model[:12]

    @property
    def model_string(self) -> str:
        return f"{self.model} v{self.version}"

    @property
    def config_model(self) -> bytes:
        return f"{self.model} {self.version}".encode()
