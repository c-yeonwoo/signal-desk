"""Launch the pinned official MCP with stock data only and narrow credentials."""
from __future__ import annotations

import argparse
import ast
import asyncio
from importlib import metadata
import json
import os
from pathlib import Path

COMMIT = "d8dac43f3e17594e8879bac3b5b333196ea69aac"
ALLOWED = frozenset({
    "get_stock_bars", "get_stock_quotes", "get_stock_trades",
    "get_stock_latest_bar", "get_stock_latest_quote", "get_stock_latest_trade",
    "get_stock_snapshot", "get_most_active_stocks",
})
KEYS = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY")


def environment(env_file: Path, inherited: dict) -> dict:
    """Do not load unrelated .env secrets, proxy URLs or endpoint overrides."""
    values = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or key.strip() not in KEYS:
            continue
        value = value.strip()
        if value.startswith(("'", '"')):
            try:
                value = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                raise ValueError("invalid credential quoting") from None
        if not isinstance(value, str):
            raise ValueError("invalid credential type")
        values[key.strip()] = value
    values.update({k: inherited[k] for k in KEYS if inherited.get(k)})
    key = values.get("APCA_API_KEY_ID") or values.get("ALPACA_API_KEY")
    secret = values.get("APCA_API_SECRET_KEY") or values.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise ValueError("Alpaca data credentials missing")
    for primary, alias in (("APCA_API_KEY_ID", "ALPACA_API_KEY"), ("APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY")):
        if values.get(primary) and values.get(alias) and values[primary] != values[alias]:
            raise ValueError("conflicting Alpaca credential aliases")
    result = {k: inherited[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
              if k in inherited}
    result.update(ALPACA_API_KEY=key, ALPACA_SECRET_KEY=secret,
                  ALPACA_TOOLSETS="stock-data", ALPACA_PAPER_TRADE="true",
                  DATA_API_URL="https://data.alpaca.markets", PYTHON_DOTENV_DISABLED="1")
    return result


def verify_install() -> None:
    dist = metadata.distribution("alpaca-mcp-server")
    origin = json.loads(dist.read_text("direct_url.json") or "{}")
    if (dist.version != "2.4.1" or origin.get("vcs_info", {}).get("commit_id") != COMMIT
            or origin.get("url") != "https://github.com/alpacahq/alpaca-mcp-server"):
        raise ValueError("install must match the reviewed Alpaca commit")


async def restrict(server) -> list[str]:
    # The upstream stock-data group also registers crypto convenience overrides.
    # Apply a server-side allowlist, not merely a client's visibility preference.
    server.enable(names=set(ALLOWED), components={"tool"}, only=True)
    tools = await server.list_tools()
    names = sorted(t.name for t in tools)
    if set(names) != ALLOWED:
        raise ValueError("unexpected stock-data tool inventory")
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--inspect", action="store_true", help="List allowed tools; no market/account requests")
    args = parser.parse_args()
    child_env = environment(args.env_file, os.environ)
    os.environ.clear()
    os.environ.update(child_env)
    verify_install()
    from alpaca_mcp_server.server import build_server
    server = build_server()
    names = asyncio.run(restrict(server))
    if args.inspect:
        print(json.dumps({"version": "2.4.1", "commit": COMMIT, "tools": names,
                          "credentials_configured": True, "account_tools": False,
                          "order_tools": False}))
    else:
        server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
