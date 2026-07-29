"""Compatibility bridge for the historical geospatial environment import."""

from roadnet_partition.io.environment import conda_prefix, initialize_geospatial_environment

initialize_geospatial_environment()

__all__ = ["conda_prefix", "initialize_geospatial_environment"]
