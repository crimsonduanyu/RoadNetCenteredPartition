from __future__ import annotations

import os
import sys
from pathlib import Path


conda_prefix = Path(os.environ.get("CONDA_PREFIX") or sys.prefix)
_initialized = False


def initialize_geospatial_environment() -> None:
    global _initialized
    if _initialized:
        return
    if os.name == "nt":
        for dll_dir in [conda_prefix, conda_prefix / "Library" / "bin"]:
            if dll_dir.exists():
                os.add_dll_directory(str(dll_dir))

    if not os.environ.get("GDAL_DATA"):
        gdal_data = conda_prefix / "Library" / "share" / "gdal"
        if (gdal_data / "header.dxf").exists():
            os.environ["GDAL_DATA"] = str(gdal_data)
    _initialized = True


initialize_geospatial_environment()

__all__ = ["conda_prefix", "initialize_geospatial_environment"]
