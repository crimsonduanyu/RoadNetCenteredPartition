"""Stage run scripts for the three-stage pipeline.

Each module reads the unified ``config.yaml`` and orchestrates one stage by
calling pure helpers in ``lib``:

- ``stage1_partition`` : spatial partitioning (regularized search).
- ``stage2_demand``    : demand dataset construction.
- ``stage3_supply``    : supply-state reconstruction.
"""
