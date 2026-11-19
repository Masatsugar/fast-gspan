"""Fast gSpan implementation using a Rust backend."""

from importlib.metadata import version, PackageNotFoundError

from .gbolt_wrapper import FastgSpan, GBoltWrapper, IncompleteResultWarning

__all__ = ["GBoltWrapper", "FastgSpan", "IncompleteResultWarning"]

try:
    __version__ = version("fast-gspan")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
