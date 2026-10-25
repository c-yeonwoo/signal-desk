"""Bounded storage maintenance must not erase quote or decision evidence."""

import sqlite3

import pytest

from signal_desk import db


def test_existing_duplicate_index_migrates_without_removing_quotes(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    c = db.conn()
    c.executemany("INSERT INTO intraday_quotes(market,ticker,ts,price) VALUES(?,?,?,?)",
                  [("kr", "005930", stamp, 70000 + stamp) for stamp in range(2000)])
    c.execute("CREATE INDEX idx_intraday_quotes_lookup ON intraday_quotes(market,ticker,ts)")
    c.execute("CREATE INDEX keep_quote_price ON intraday_quotes(price)")
    before = c.execute("SELECT * FROM intraday_quotes ORDER BY market,ticker,ts").fetchall()
    free_before = c.execute("PRAGMA freelist_count").fetchone()[0]
    c.commit()
    c.close()
    db._schema_ready.discard(str(db.DB.resolve()))

    c = db.conn()  # The existing-DB migration, not just the helper.
    assert c.execute("SELECT * FROM intraday_quotes ORDER BY market,ticker,ts").fetchall() == before
    names = {row[1] for row in c.execute("PRAGMA index_list(intraday_quotes)")}
    assert "idx_intraday_quotes_lookup" not in names
    assert "keep_quote_price" in names
    assert "sqlite_autoindex_intraday_quotes_1" in names
    assert c.execute("PRAGMA freelist_count").fetchone()[0] > free_before
    plan = c.execute("EXPLAIN QUERY PLAN SELECT ts,price FROM intraday_quotes "
                     "WHERE market=? AND ticker=? AND ts>=? AND ts<=? ORDER BY ts",
                     ("kr", "005930", 100, 200)).fetchall()
    assert any("sqlite_autoindex_intraday_quotes_1" in row[-1] for row in plan)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO intraday_quotes(market,ticker,ts,price) VALUES('kr','005930',0,1)")
    c.rollback()
    assert db._drop_duplicate_intraday_quote_index(c) is False
    c.close()


@pytest.mark.parametrize("definition", [
    "ON intraday_quotes(market,ticker,ts) WHERE ts>0",
    "ON intraday_quotes(market COLLATE NOCASE,ticker,ts)",
    "ON intraday_quotes(market,ticker,ts DESC)",
    "ON intraday_quotes(market,ticker,ts,price)",
    "ON intraday_quotes(ticker,market,ts)",
    "ON intraday_quotes(lower(market),ticker,ts)",
])
def test_modified_named_index_is_preserved(definition):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE intraday_quotes(market TEXT,ticker TEXT,ts INTEGER,price REAL, "
              "PRIMARY KEY(market,ticker,ts))")
    c.execute("CREATE INDEX idx_intraday_quotes_lookup " + definition)
    assert db._drop_duplicate_intraday_quote_index(c) is False
    assert db._intraday_quote_lookup_index(c)["present"] is True
    c.close()


@pytest.mark.parametrize("primary", ["", ", PRIMARY KEY(ticker,market,ts)"])
def test_no_matching_primary_key_means_no_drop(primary):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE intraday_quotes(market TEXT,ticker TEXT,ts INTEGER,price REAL" + primary + ")")
    c.execute("CREATE INDEX idx_intraday_quotes_lookup ON intraday_quotes(market,ticker,ts)")
    assert db._drop_duplicate_intraday_quote_index(c) is False
    c.close()


def test_unique_named_index_is_not_this_migrations_target():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE intraday_quotes(market TEXT,ticker TEXT,ts INTEGER,price REAL, "
              "PRIMARY KEY(market,ticker,ts))")
    c.execute("CREATE UNIQUE INDEX idx_intraday_quotes_lookup ON intraday_quotes(market,ticker,ts)")
    assert db._drop_duplicate_intraday_quote_index(c) is False
    c.close()


def test_prune_only_expired_cache_and_readonly_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(db.time, "time", lambda: 100)
    c = db.conn()
    c.executemany("INSERT INTO advisor_prompt_cache VALUES(?,?,?,?)",
                  [("old", '{"picks":[]}', 0, 99), ("edge", '{"picks":[]}', 0, 100),
                   ("active", '{"picks":["AAA"]}', 0, 101)])
    c.execute("INSERT INTO advisor_prompt_events(ts,uid,market,style,stage,input_hash,outcome,latency_ms) "
              "VALUES(1,900001,'kr','balanced','primary','old','provider_call',1)")
    c.execute("INSERT INTO execution_events(event_key,market,ticker,event_type,ts) "
              "VALUES('keep','kr','005930','test',1)")
    c.execute("INSERT INTO intraday_quotes(market,ticker,ts,price) VALUES('kr','005930',1,70000)")
    c.commit()
    before = c.execute("SELECT * FROM advisor_prompt_cache ORDER BY input_hash").fetchall()
    c.close()

    report = db.storage_breakdown()
    assert report["advisor_prompt_cache"] == {"expired_rows": 2, "active_rows": 1}
    assert report["intraday_quote_lookup_index"] == {
        "present": False, "duplicate_of_primary_key": False, "primary_key_lookup_available": True}
    c = db.conn()
    assert c.execute("SELECT * FROM advisor_prompt_cache ORDER BY input_hash").fetchall() == before
    c.close()
    assert db.advisor_prompt_cache_prune() == 2
    assert db.advisor_prompt_cache_prune(now=100) == 0
    assert db.advisor_prompt_get("active", now=100) == {"picks": ["AAA"]}
    c = db.conn()
    for table in ("advisor_prompt_events", "execution_events", "intraday_quotes"):
        assert c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 1
    assert c.execute("SELECT input_hash FROM advisor_prompt_cache").fetchall() == [("active",)]
    c.close()
