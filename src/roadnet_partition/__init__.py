"""Road-network-centered partitioning package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("roadnet-partition")
except PackageNotFoundError:  # Source checkout before installation.
    __version__ = "0+unknown"

__all__ = ["__version__"]
