from __future__ import annotations


def test_duckdb_runtime_version_and_arrow_batches() -> None:
    import duckdb

    assert duckdb.__version__ == "1.5.5"

    connection = duckdb.connect()
    try:
        relation = connection.sql(
            "SELECT value FROM (VALUES (2), (1)) AS data(value) ORDER BY value"
        )
        assert relation.fetchall() == [(1,), (2,)]
        batches = list(relation.to_arrow_reader(batch_size=1))
        assert [batch.column("value").to_pylist() for batch in batches] == [[1], [2]]
    finally:
        connection.close()
