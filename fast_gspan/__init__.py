"""Fast gSpan implementation using gBolt C++ backend."""

from importlib.metadata import version, PackageNotFoundError

from .gbolt_wrapper import FastgSpan, GBoltWrapper

__all__ = ["GBoltWrapper", "FastgSpan"]

try:
    __version__ = version("fast-gspan")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
