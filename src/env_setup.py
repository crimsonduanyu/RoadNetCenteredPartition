"""Compatibility bridge for the historical geospatial environment import."""

from roadnet_partition.io import environment as _environment

_environment.initialize_geospatial_environment()
conda_prefix = _environment.conda_prefix

__all__ = ["conda_prefix", "initialize_geospatial_environment"]
initialize_geospatial_environment = _environment.initialize_geospatial_environment

if hasattr(_environment, "gdal_data"):
    gdal_data = _environment.gdal_data
    __all__.append("gdal_data")
