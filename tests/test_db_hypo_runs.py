"""이슈 흐름 이력 저장소는 기본 SQLite tuple row와 호환돼야 한다."""

from signal_desk import db


def test_hypo_runs_recent_reads_default_sqlite_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db._schema_ready.clear()
    db.hypo_run_insert({
        "built_at": "2026-10-05T09:00:00+09:00",
        "as_of": "2026-10-05",
        "source": "llm",
        "model": "test-model",
        "sectors": ["semiconductor"],
        "tickers": ["005930"],
        "tree": {"kind": "root"},
    })

    rows = db.hypo_runs_recent()

    assert rows == [{
        "id": 1,
        "built_at": "2026-10-05T09:00:00+09:00",
        "as_of": "2026-10-05",
        "source": "llm",
        "model": "test-model",
        "sectors": ["semiconductor"],
        "tickers": ["005930"],
    }]
