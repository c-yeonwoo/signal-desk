"""KB 예산 분리 — 트레이딩 자동 Sonnet off, 학습은 수동."""

import datetime

from signal_desk import api, config


def test_kb_auto_collect_default_off(monkeypatch):
    monkeypatch.delenv("KB_AUTO_COLLECT", raising=False)
    assert config.kb_auto_collect() is False
    monkeypatch.setenv("KB_AUTO_COLLECT", "1")
    assert config.kb_auto_collect() is True
    monkeypatch.setenv("KB_AUTO_COLLECT", "true")
    assert config.kb_auto_collect() is True
    monkeypatch.setenv("KB_AUTO_COLLECT", "0")
    assert config.kb_auto_collect() is False


def test_daily_kb_collect_skips_llm_when_flag_off(monkeypatch):
    monkeypatch.setattr(api.config, "kb_auto_collect", lambda: False)
    monkeypatch.setattr(api.db, "kv_get", lambda k: None)
    set_keys: list[tuple[str, str]] = []
    monkeypatch.setattr(api.db, "kv_set", lambda k, v: set_keys.append((k, v)))
    called: list[str] = []
    monkeypatch.setattr(api.kb, "collect_fanding", lambda: called.append("fanding") or {})
    monkeypatch.setattr(api.kb, "collect_outstanding", lambda: called.append("outstanding") or {})
    monkeypatch.setattr(api.kb, "collect_youtube", lambda: called.append("youtube") or {})
    monkeypatch.setattr(api.kb, "collect_rss_macro", lambda: called.append("rss") or {})
    monkeypatch.setattr(api, "_kb_targets", lambda: called.append("targets") or [])
    monkeypatch.setattr(api.kb, "refresh", lambda t: called.append("refresh") or {})
    monkeypatch.setattr(api.store, "load_us_universe", lambda: [])
    api._daily_kb_collect()
    assert called == []                                 # LLM 수집 경로 미진입
    assert set_keys and set_keys[0][0] == "kb_collect_date"


def test_daily_kb_collect_runs_when_flag_on(monkeypatch):
    monkeypatch.setattr(api.config, "kb_auto_collect", lambda: True)
    monkeypatch.setattr(api.db, "kv_get", lambda k: None)
    monkeypatch.setattr(api.db, "kv_set", lambda k, v: None)
    called: list[str] = []
    monkeypatch.setattr(api.kb, "collect_fanding", lambda: called.append("fanding") or {})
    monkeypatch.setattr(api.kb, "collect_outstanding", lambda: called.append("outstanding") or {})
    monkeypatch.setattr(api.kb, "collect_youtube", lambda: called.append("youtube") or {})
    monkeypatch.setattr(api.kb, "collect_rss_macro", lambda: called.append("rss") or {})
    monkeypatch.setattr(api, "_kb_targets", lambda: [])
    monkeypatch.setattr(api.store, "load_us_universe", lambda: [])
    api._daily_kb_collect()
    assert called == ["fanding", "outstanding", "youtube", "rss"]


def test_about_moves_skipped_on_weekend(monkeypatch):
    """주말 bot 루프에서 about/moves Haiku drip이 돌지 않는다."""
    ran: list[str] = []
    monkeypatch.setattr(api, "_daily_kb_collect", lambda: None)
    monkeypatch.setattr(api, "_morning_digest", lambda: False)
    monkeypatch.setattr(api.db, "user_bots_enabled", lambda: [])
    monkeypatch.setattr(api, "_open_markets", lambda: [])
    monkeypatch.setattr(api, "_backfill_us_prices_batch", lambda n: {"filled": 0, "missing": 0})
    monkeypatch.setattr(api, "_refresh_us_prices_stale", lambda n: {"filled": 0, "stale": 0})
    monkeypatch.setattr(api, "_backfill_about_batch", lambda n: ran.append("about") or 0)
    monkeypatch.setattr(api, "_backfill_moves_batch", lambda n: ran.append("moves") or 0)
    monkeypatch.setattr(api.db, "uids_with_ticker_favorites", lambda: [])
    monkeypatch.setattr(api.db, "kv_get", lambda k: "already")  # daily maintenance 스킵
    monkeypatch.setattr(api, "_kst_now",
                        lambda: datetime.datetime(2026, 8, 2, 12, 0))  # 일요일
    api._bot_loop_iteration()
    assert ran == []


def test_youtube_batches_macro_digest(monkeypatch):
    """유튜브는 영상마다 Sonnet digest를 돌리지 않고 배치 끝 1회."""
    from signal_desk import kb
    from signal_desk import config as cfg

    monkeypatch.setattr(cfg, "youtube_key", lambda: "k")
    monkeypatch.setattr(cfg, "youtube_channels", lambda: ["ch"])
    monkeypatch.setattr(cfg, "youtube_max_per_channel", lambda: 2)
    monkeypatch.setattr(kb.db, "kb_document_urls", lambda source="insight": set())
    monkeypatch.setattr(kb.db, "kb_source_ensure", lambda *a, **k: {"lifecycle": "active", "enabled": True})
    monkeypatch.setattr(kb.db, "kb_sources_bump_run", lambda *a, **k: None)
    monkeypatch.setattr(kb.db, "kb_sources_touch", lambda *a, **k: None)
    monkeypatch.setattr(kb, "evaluate_source_quality", lambda sk: {})
    monkeypatch.setattr(kb, "_macro_source_summary", lambda t, r: "요약")
    monkeypatch.setattr(kb, "_year_ok", lambda p: True)

    class Y:
        @staticmethod
        def channel_videos(handle, max_results=5):
            return {"channel": "ch", "videos": [
                {"title": "a", "video_id": "v1", "published": "2026-08-01", "description": "x" * 80},
                {"title": "b", "video_id": "v2", "published": "2026-08-02", "description": "y" * 80},
            ]}

        @staticmethod
        def video_url(vid):
            return f"https://youtu.be/{vid}"

        @staticmethod
        def transcript(vid):
            return "자막 내용 " * 40

    monkeypatch.setitem(__import__("sys").modules, "signal_desk.ingest.youtube", Y)
    # import inside collect uses from signal_desk.ingest import youtube
    import signal_desk.ingest.youtube as yt_mod
    monkeypatch.setattr(yt_mod, "channel_videos", Y.channel_videos)
    monkeypatch.setattr(yt_mod, "video_url", Y.video_url)
    monkeypatch.setattr(yt_mod, "transcript", Y.transcript)

    rebuilds: list[bool] = []
    digests = {"n": 0}

    def fake_import(*a, **k):
        rebuilds.append(bool(k.get("rebuild", True)))
        return {"ok": True}

    monkeypatch.setattr(kb, "import_macro", fake_import)
    monkeypatch.setattr(kb, "_rebuild_macro_digest", lambda: digests.__setitem__("n", digests["n"] + 1))

    out = kb.collect_youtube()
    assert out["ok"] and len(out["macro"]) == 2
    assert rebuilds == [False, False]
    assert digests["n"] == 1
