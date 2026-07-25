"""테스트 전역 격리 — 외부 자격증명을 빈 값으로 고정한다.

`signal_desk.api`는 import 시 `config.load_env()`로 리포의 `.env`를 `os.environ`에 올린다.
그래서 api를 import하는 테스트가 한 번이라도 돌면 이후 모든 테스트에서 실제 키가 살아나고,
`llm.available()`이 True가 되어 봇 테스트가 **실제 Anthropic API를 호출**했다(비용·네트워크
의존·비결정성). 여기서 키를 빈 문자열로 먼저 심어두면 `load_env()`의 `setdefault`가
덮어쓰지 못해 테스트는 항상 오프라인·결정론으로 돈다.

특정 키가 있는 경로를 검증하는 테스트는 `monkeypatch.setenv`로 각자 켜면 된다(그쪽이 우선).
"""

import os

# 실제 호출이 나갈 수 있는 모든 외부 자격증명. 값이 존재하되 빈 문자열이어야 setdefault가 무력화된다.
_BLANK_KEYS = (
    "ANTHROPIC_API_KEY",
    "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT", "TOSS_ACCOUNT_OWNER",
    "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO",
    "KRX_API_KEY", "DART_API_KEY", "ECOS_API_KEY", "FRED_API_KEY",
    "ALPHAVANTAGE_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET",
    "YOUTUBE_API_KEY", "TYPECAST_API_KEY",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "FANDING_TT", "FANDING_DEVICE_UID", "OUTSTANDING_COOKIE",
)

for _k in _BLANK_KEYS:
    os.environ[_k] = ""
