"""Private Telegram chat binding and read-only commands.

getUpdates is used only when no webhook owns the bot. Never delete an existing webhook:
that could steal updates from a separately operated bot integration.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.request
from functools import lru_cache

from signal_desk import bot, config, db, notify

log = logging.getLogger("signal_desk.telegram_inbound")
_OFFSET_KEY = "telegram_inbound_offset"
_STATUS_KEY = "telegram_inbound_status"
_CODE_TTL = 600
_STYLES = {"conservative": "안정형", "balanced": "균형형", "aggressive": "공격형"}


def _frozen_guidance(uid: int, market: str) -> tuple[dict | None, list[dict]]:
    row = db.portfolio_snapshot_latest(uid, market)
    return row, (row.get("payload", {}).get("guidance") or []) if row else []


def daily_summary(uid: int, market: str, date: str) -> str | None:
    """Only a same-session frozen close snapshot can create a personal digest."""
    row, guidance = _frozen_guidance(uid, market)
    if not row or row["source"] != "daily_close" or row["as_of"] != date:
        return None
    lines = [f"📌 내 포트폴리오 마감 요약 · {date} · {market.upper()}",
             f"저장된 진단 품질: {row['data_quality']} · 시세 기준일 {row['as_of']}"]
    owner = config.toss_account_owner()
    owner_user = db.user_by_email(owner) if owner else None
    if owner_user and owner_user["id"] == uid:
        observations = db.account_observations_daily(uid, broker="toss", market=market, limit=2)
        latest = observations[-1] if observations else None
        if (latest and latest["date"] == date and latest["quality"] == "holdings_daily_provisional"
                and latest["daily_return_pct"] is not None):
            pnl = latest.get("daily_pnl")
            unit = "원" if market == "kr" else "$"
            lines.append(f"실보유 주식 당일손익(잠정): {pnl}{unit} · {latest['daily_return_pct']:+.2f}%")
        else:
            lines.append("실보유 주식 당일손익: 확인 불가")
        lines.append("현금·입출금·실현손익 미포함 — 계좌 전체 수익률 아님")
    else:
        lines.append("입력 보유정보의 진단이며 실계좌 손익은 포함하지 않습니다.")
    if guidance:
        item = guidance[0]
        lines.append(f"우선 검토: {str(item.get('action') or '확인 필요')[:50]} — {str(item.get('reason') or '')[:130]}")
    else:
        lines.append("새로운 검토 항목 없음")
    lines.append("기준선·거래비용·기여도는 검증 가능한 관측이 없어 표시하지 않습니다. /next 로 저장된 검토 항목을 확인하세요.")
    return "\n".join(lines)


def enqueue_daily_summaries(date: str, market: str = "kr", *, now: int | None = None) -> int:
    at = int(time.time()) if now is None else now
    count = 0
    for link in db.telegram_links_all():
        if market not in link["markets"] or "daily_summary" not in link["alert_types"]:
            continue
        text = daily_summary(link["uid"], market, date)
        if text and notify.enqueue_user(link["uid"], text, dedupe_key=f"daily-summary:{market}:{date}",
                                        market=market, alert_type="daily_summary", now=at,
                                        expires_at=at + 12 * 3600):
            count += 1
    return count


def issue_code(uid: int, *, now: int | None = None) -> dict:
    at = int(time.time()) if now is None else now
    code = secrets.token_hex(6).upper()
    db.telegram_link_code_issue(uid, hashlib.sha256(code.encode()).hexdigest(), now=at,
                                ttl=_CODE_TTL)
    return {"code": code, "expires_at": at + _CODE_TTL}


def _bot_api(method: str, payload: dict) -> dict:
    token = config.telegram_token()
    if not token:
        raise RuntimeError("telegram token not configured")
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError("telegram api returned not ok")
    return result


@lru_cache(maxsize=1)
def bot_username() -> str | None:
    if not config.telegram_token():
        return None
    try:
        name = _bot_api("getMe", {}).get("result", {}).get("username")
        return name if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_]{5,32}", name) else None
    except Exception as exc:  # noqa: BLE001 — settings must still be usable without Telegram
        log.warning("Telegram bot username lookup failed: %s", type(exc).__name__)
        return None


def _performance(link: dict, market: str) -> str:
    style = link["style"]
    uid = next((key for key, value in bot.REFERENCE_BOTS.items() if value == style), None)
    if uid is None:
        return "선택한 봇을 찾지 못했습니다. 앱에서 설정을 확인하세요."
    data = bot.performance(uid, market)
    if not data.get("days"):
        return f"{_STYLES[style]} {market.upper()} 페이퍼 성과는 아직 일별 관측이 부족합니다."
    ret = data.get("return_pct")
    value = f"{ret:+.2f}%" if isinstance(ret, (float, int)) else "계산 불가"
    lines = [f"{_STYLES[style]} {market.upper()} 페이퍼 · 시드 대비 {value}",
             f"관측 {data['days']}일 · 거래 {data['n_trades']}건 · MDD {data['max_drawdown_pct']:.2f}%",
             "실계좌 수익률이 아닙니다. 비교 기준과 비용은 앱 성과 화면에서 확인하세요."]
    owner = config.toss_account_owner()
    owner_user = db.user_by_email(owner) if owner else None
    if owner_user and owner_user["id"] == link["uid"]:
        observations = db.account_observations_daily(link["uid"], broker="toss", market=market, limit=1)
        if observations:
            latest = observations[-1]
            lines.append(f"실보유 최근 관측 {latest['date']}: 평가액 {latest['holdings_value']} {latest['currency']}")
            lines.append("현금·입출금·실현손익 미포함 — 계좌 전체 수익률 아님")
    return "\n".join(lines)


def command_response(chat_id: str, message: str, *, now: int | None = None,
                     chat_label: str = "") -> str | None:
    at = int(time.time()) if now is None else now
    parts = (message or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    command = parts[0].split("@", 1)[0].lower()
    if command == "/link":
        if len(parts) != 2 or not re.fullmatch(r"[0-9A-Fa-f]{12}", parts[1]):
            return "앱 마이페이지에서 10분 유효한 연결 코드를 발급한 뒤 /link 코드 를 보내세요."
        digest = hashlib.sha256(parts[1].upper().encode()).hexdigest()
        if db.telegram_link_code_claim(digest, chat_id, now=at, chat_label=chat_label):
            return (f"코드를 확인했습니다. 이 채팅 ID는 {chat_id}입니다. "
                    "로그인된 앱 마이페이지에서 채팅 ID를 대조하고 ‘연결 확정’을 눌러야 개인 알림이 시작됩니다.")
        return "코드가 만료되었거나 이미 다른 채팅에서 사용 중입니다. 앱에서 새 코드를 발급하세요."
    link = db.telegram_link_by_chat(chat_id)
    if not link:
        return "이 채팅은 앱 계정에 연결되지 않았습니다. 앱 마이페이지에서 연결하세요."
    if command in ("/start", "/help"):
        return "읽기 전용 명령: /status · /performance kr|us · /why 종목코드 · /next\n설정·연결 해제는 앱 마이페이지에서만 가능합니다."
    if command == "/status":
        return (f"연결됨 · {_STYLES[link['style']]} 페이퍼 봇 · {', '.join(m.upper() for m in link['markets'])}\n"
                f"알림: {', '.join(link['alert_types']) or '끔'}\n"
                "실주문이나 한도 변경은 이 채팅에서 할 수 없습니다.")
    if command == "/performance":
        market = parts[1].lower() if len(parts) == 2 else (link["markets"][0] if link["markets"] else "kr")
        if market not in ("kr", "us"):
            return "사용법: /performance kr 또는 /performance us"
        return _performance(link, market)
    if command == "/why":
        if len(parts) != 2 or not re.fullmatch(r"[A-Za-z0-9.]{1,15}", parts[1]):
            return "사용법: /why 종목코드"
        ticker = parts[1].upper()
        market = "kr" if ticker.isdigit() else "us"
        row, guidance = _frozen_guidance(link["uid"], market)
        match = next((g for g in guidance if ticker == g.get("ticker") or ticker in (g.get("tickers") or [])), None)
        if match and row:
            return (f"{ticker} · 저장된 내 포트폴리오 진단 {row['as_of']}\n"
                    f"{str(match.get('action') or '검토')[:60]}: {str(match.get('reason') or '')[:240]}\n"
                    "현재 시세에 대한 새 매매 판정이 아닙니다.")
        return f"{ticker}의 저장된 개인 검토 항목이 없습니다. 앱 종목 상세에서 판정 근거·가격 시점을 확인하세요."
    if command == "/next":
        lines = []
        for market in link["markets"]:
            row, guidance = _frozen_guidance(link["uid"], market)
            if row:
                lines.append(f"{market.upper()} 저장 진단 {row['as_of']} · {row['data_quality']}")
                lines.extend(f"· {str(g.get('action') or '검토')[:50]}: {str(g.get('reason') or '')[:110]}"
                             for g in guidance[:2])
        if not lines:
            return "저장된 포트폴리오 진단이 없습니다. 앱에서 분석을 먼저 실행하세요."
        return "\n".join(lines) + "\n과거 입력·가격 기준의 검토 항목이며 새 주문 지시가 아닙니다."
    return "알 수 없는 명령입니다. /help 를 입력하세요."


def handle_update(update: dict, *, now: int | None = None) -> bool:
    """Private user chat only. Group/channel messages are ignored without echoing private state."""
    msg = update.get("message") or {}
    chat, sender = msg.get("chat") or {}, msg.get("from") or {}
    if (chat.get("type") != "private" or sender.get("is_bot") or
            not isinstance(chat.get("id"), int) or sender.get("id") != chat.get("id")):
        return False
    name = str(sender.get("username") or sender.get("first_name") or "Telegram 사용자")
    text = command_response(str(chat["id"]), msg.get("text") or "", now=now,
                            chat_label=name)
    if not text:
        return False
    return notify._send_one(config.telegram_token(), str(chat["id"]), text)


def poll_once() -> int:
    """Offset is durable; a failed reply is retried, and duplicate link claims are idempotent."""
    if not config.telegram_token():
        return 0
    offset = db.kv_get(_OFFSET_KEY) or 0
    try:
        result = _bot_api("getUpdates", {"offset": offset, "limit": 30, "timeout": 0,
                                         "allowed_updates": ["message"]})
    except urllib.error.HTTPError as exc:
        status = "webhook_or_conflict" if exc.code == 409 else f"http_{exc.code}"
        db.kv_set(_STATUS_KEY, status)
        log.warning("Telegram inbound unavailable: HTTP %s", exc.code)
        return 0
    except Exception as exc:  # noqa: BLE001 — background loop must remain alive
        db.kv_set(_STATUS_KEY, "unavailable")
        log.warning("Telegram inbound unavailable: %s", type(exc).__name__)
        return 0
    db.kv_set(_STATUS_KEY, "polling")
    count = 0
    for update in result.get("result") or []:
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or update_id < offset:
            continue
        # 미인식/비공개 채팅 이외의 업데이트도 확인 처리한다. 답변 전송 실패 시에는
        # 사용자가 명령을 다시 보낼 수 있고, 연결 claim은 멱등이다.
        handle_update(update)
        offset = update_id + 1
        db.kv_set(_OFFSET_KEY, offset)
        count += 1
    return count


def inbound_status() -> str:
    if not config.telegram_token():
        return "unconfigured"
    return db.kv_get(_STATUS_KEY) or "starting"
