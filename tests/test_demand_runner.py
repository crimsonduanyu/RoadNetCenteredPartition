from __future__ import annotations

from pathlib import Path

from roadnet_partition.config import ResolvedStageConfig
from roadnet_partition.downstream import demand
from roadnet_partition.pipeline.results import RunContext, StageStatus


def test_run_demand_uses_context_stage_dir(tmp_path: Path, monkeypatch) -> None:
    calls = []
    def fake_run(config):
        calls.append(config)
        output = Path(config["order_pipeline"]["outputs"]["root"])
        output.mkdir(parents=True, exist_ok=True)
        (output / "metadata.json").write_text(
            '{"order_stats":{"staged_rows":3},"service_type_counts":{"exclusive":2,"carpool":1},"num_clusters":2,"num_tensor_slots":1}',
            encoding="utf-8",
        )
    monkeypatch.setattr(demand, "run_from_config", fake_run)
    config = ResolvedStageConfig(tmp_path / "config.yaml", {"order_pipeline": {"outputs": {}, "time_slot_minutes": 10}}, "fingerprint")
    context = RunContext("run", tmp_path / "run", tmp_path).for_stage("demand")
    result = demand.run_demand(config, context)
    assert result.status is StageStatus.COMPLETE
    assert calls[0]["order_pipeline"]["outputs"]["root"] == str(context.stage_dir)
    assert result.metrics == {"orders": 3, "exclusive": 2, "carpool": 1, "clusters": 2, "tensor_slots": 1}
