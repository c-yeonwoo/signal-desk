"""RSS/Atom 파서 + 거시 KB RSS 수집(증분·요약)."""

from signal_desk import config, kb, llm
from signal_desk.ingest import rss

_RSS2 = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
<item><title>Post A</title><link>https://ex.com/a</link>
<pubDate>Wed, 09 Jul 2026 12:00:00 GMT</pubDate>
<description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt; macro view here.&lt;/p&gt;</description></item>
</channel></rss>"""

_ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title>
<entry><title>Entry B</title><link rel="alternate" href="https://ex.com/b"/>
<published>2026-07-08T00:00:00Z</published>
<summary>Some &lt;i&gt;valuation&lt;/i&gt; commentary text here.</summary></entry>
</feed>"""


class _Resp:
    def __init__(self, b): self._b = b
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_parse_rss2(monkeypatch):
    monkeypatch.setattr(rss.urllib.request, "urlopen", lambda req, timeout=0: _Resp(_RSS2))
    out = rss.feed_entries("http://x")
    assert len(out) == 1
    e = out[0]
    assert e["title"] == "Post A" and e["url"] == "https://ex.com/a"
    assert "world" in e["summary"] and "<" not in e["summary"]   # 태그 제거


def test_parse_atom(monkeypatch):
    monkeypatch.setattr(rss.urllib.request, "urlopen", lambda req, timeout=0: _Resp(_ATOM))
    e = rss.feed_entries("http://x")[0]
    assert e["url"] == "https://ex.com/b" and e["published"].startswith("2026")
    assert "valuation" in e["summary"]


def test_feed_entries_graceful_on_error(monkeypatch):
    def boom(*a, **k): raise OSError("net down")
    monkeypatch.setattr(rss.urllib.request, "urlopen", boom)
    assert rss.feed_entries("http://x") == []


def test_collect_rss_macro_incremental(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "macro_rss_feeds", lambda: [{"name": "Test", "url": "http://x"}])
    monkeypatch.setattr(rss, "feed_entries", lambda url, limit=5: [
        {"title": "Macro note", "url": "http://x/1", "published": "2026-07-09", "summary": "A" * 80}])
    monkeypatch.setattr(llm, "available", lambda: False)   # 요약·다이제스트 LLM 스킵(결정론)
    out = kb.collect_rss_macro(force=True)
    assert out["ok"] and len(out["macro"]) == 1
    out2 = kb.collect_rss_macro()                          # 재실행 → seen으로 스킵
    assert len(out2["macro"]) == 0
