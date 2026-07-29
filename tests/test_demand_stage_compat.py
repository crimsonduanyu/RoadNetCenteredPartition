from __future__ import annotations

from stages import stage2_demand
from roadnet_partition.downstream import demand


def test_stage2_preserves_default_and_explicit_config_forwarding(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(demand, "main", lambda argv: calls.append(argv))
    stage2_demand.main([])
    stage2_demand.main(["fixture.yaml"])
    assert calls == [[str(stage2_demand.CONFIG_PATH)], ["fixture.yaml"]]
