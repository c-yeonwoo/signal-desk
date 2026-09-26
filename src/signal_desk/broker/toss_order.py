"""토스 지정가 주문의 단발 전송기. 호출자는 제출 상태를 먼저 영속화해야 한다.

어떤 HTTP/네트워크 오류에서도 재시도하지 않는다. 결과가 불명확하면 UNKNOWN으로 남긴다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from signal_desk.ingest import toss


def submit_limit(account: str, intent: dict) -> dict:
    """이미 PREPARED→SUBMITTING으로 선점한 intent를 정확히 한 번만 전송."""
    if intent.get("status") != "SUBMITTING" or intent.get("account_seq") != account:
        raise ValueError("intent is not claimed for this broker account")
    token = toss._access_token()
    if not token:
        return {"accepted": False, "unknown": True, "reason": "broker token unavailable"}
    payload = {"clientOrderId": intent["client_order_id"], "symbol": intent["symbol"],
               "side": intent["side"], "orderType": "LIMIT", "timeInForce": "DAY",
               "quantity": intent["quantity"], "price": intent["limit_price"]}
    request = urllib.request.Request(
        toss._BASE + "/api/v1/orders", data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"authorization": "Bearer " + token, "X-Tossinvest-Account": account,
                 "content-type": "application/json", "accept": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
            UnicodeError, ValueError):
        return {"accepted": False, "unknown": True, "reason": "broker submission outcome unknown"}
    result = body.get("result") if isinstance(body, dict) else None
    if (not isinstance(result, dict) or not isinstance(result.get("orderId"), str)
            or not result["orderId"] or result.get("clientOrderId") != intent["client_order_id"]):
        return {"accepted": False, "unknown": True, "reason": "broker acknowledgement invalid"}
    return {"accepted": True, "unknown": False, "order_id": result["orderId"]}
