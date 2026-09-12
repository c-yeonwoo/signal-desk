"""알림 전달(텔레그램) — in-app 알림을 폰으로 능동 푸시. 시그널 변동·봇 체결·악재 감지 등.

키는 .env(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, 콤마로 여러 채팅). 키 없으면 조용히 no-op
(in-app 알림은 그대로). 표준 라이브러리(urllib)만 사용.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from signal_desk import config, db

log = logging.getLogger("signal_desk.notify")

_TIMEOUT = 10


def available() -> bool:
    return bool(config.telegram_token() and config.telegram_chat_ids())


def push(text: str) -> bool:
    """텔레그램으로 메시지 전송(설정된 모든 채팅). 성공 1건 이상이면 True. 미설정/실패 시 False(그레이스풀)."""
    tok = config.telegram_token()
    chats = config.telegram_chat_ids()
    text = (text or "").strip()
    if not tok or not chats or not text:
        return False
    ok = False
    for chat in chats:
        body = json.dumps({"chat_id": chat, "text": text[:4000],
                           "disable_web_page_preview": True}).encode("utf-8")
        req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage",
                                     data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT):
                ok = True
        except urllib.error.HTTPError as e:
            log.warning("텔레그램 전송 실패: HTTP %s", e.code)
        except Exception as e:
            log.warning("텔레그램 전송 실패: %s", type(e).__name__)
    return ok


def enqueue(text: str, *, dedupe_key: str, priority: str = "normal",
            expires_at: int | None = None, now: int | None = None) -> bool:
    """전송할 이벤트를 먼저 DB 아웃박스에 적재한다.

    반환값은 이번 호출이 새 이벤트를 넣었는지다. 실제 전송은 :func:`drain`이 담당하므로
    Telegram이 순간 실패하거나 프로세스가 죽어도 중요한 매매·시그널 알림이 사라지지 않는다.
    """
    text = (text or "").strip()
    if not text or not dedupe_key:
        return False
    return db.notification_enqueue(dedupe_key, text[:4000], priority=priority, expires_at=expires_at, now=now)


def _retry_delay_seconds(attempts: int) -> int:
    """짧은 장애는 빨리 회복하고 장기 장애에서는 API를 두드리지 않는 capped exponential backoff."""
    return min(3600, 30 * (2 ** min(max(attempts, 0), 7)))


def drain(*, now: int | None = None, limit: int = 20) -> dict[str, int]:
    """기한이 남은 due 메시지를 전송하고 원장의 상태를 갱신한다.

    설정이 빠진 개발 환경에서는 attempt를 올리지 않는다. 설정만 나중에 주입해도 pending
    사건을 정상 전달할 수 있고, 매 루프마다 실패 이력만 쌓이는 것을 피한다.
    """
    at = int(time.time()) if now is None else int(now)
    stats = {"sent": 0, "failed": 0, "expired": 0, "pending": 0}
    for item in db.notification_outbox_due(now=at, limit=limit):
        expires_at = item.get("expires_at")
        if expires_at is not None and expires_at <= at:
            db.notification_outbox_expire(item["id"])
            stats["expired"] += 1
            continue
        if not available():
            stats["pending"] += 1
            break
        if push(item["text"]):
            db.notification_outbox_sent(item["id"], now=at)
            stats["sent"] += 1
        else:
            delay = _retry_delay_seconds(item["attempts"])
            db.notification_outbox_failed(item["id"], next_attempt=at + delay,
                                          error="telegram delivery failed")
            stats["failed"] += 1
    return stats
