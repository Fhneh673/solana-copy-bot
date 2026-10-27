"""WebSocket-слушатель кошельков-целей (logsSubscribe) + догон пропущенного после переподключения."""
import asyncio
import json
import logging
import time

from ..strategy.parser import parse_swap

log = logging.getLogger(__name__)

CATCHUP_LIMIT = 25        # сколько последних транзакций кошелька просматриваем после обрыва
CATCHUP_MAX_BUY_AGE = 60  # покупки старше N секунд при догоне не копируем (цена уже ушла)


async def watch(cfg, session, rpc, trader):
    seen: dict[str, None] = {}      # упорядоченный набор: при переполнении забываем только самые старые
    tasks: set = set()
    last_sig: dict[str, str] = {}   # кошелёк -> последняя увиденная подпись (для догона)

    async def handle(wallet: str, sig: str, catchup: bool = False):
        key = f"{wallet}:{sig}"   # как в Rust-ядре: два отслеживаемых кошелька в одной транзакции не мешают друг другу
        if key in seen:
            return
        seen[key] = None
        if len(seen) > 5000:
            for old in list(seen)[:2500]:
                del seen[old]
        tx = await rpc.get_tx(sig)
        if not tx:
            return
        t = parse_swap(tx, wallet)
        if not t:
            return
        if catchup and t["side"] == "buy" and tx.get("blockTime") \
                and time.time() - tx["blockTime"] > CATCHUP_MAX_BUY_AGE:
            log.info("догон: пропускаю старую покупку %s (%ds назад)", t["mint"][:6], time.time() - tx["blockTime"])
            return
        log.info("цель %s..: %s %s на %.3f SOL%s", wallet[:6], t["side"], t["mint"][:6], t["sol"],
                 " (догон)" if catchup else "")
        try:
            await trader.copy(wallet, t)
        except Exception as e:  # noqa: BLE001
            log.error("copy failed: %s", e)

    def spawn(coro):
        t = asyncio.create_task(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    async def catch_up(wallet: str):
        """Что цель сделала, пока мы переподключались: берём подписи новее последней увиденной."""
        until = last_sig.get(wallet)
        if not until:
            return
        try:
            rows = await rpc.call("getSignaturesForAddress", [wallet, {
                "limit": CATCHUP_LIMIT, "until": until, "commitment": "confirmed"}])
        except Exception as e:  # noqa: BLE001
            log.warning("догон %s..: %s", wallet[:6], e)
            return
        rows = [r for r in rows if not r.get("err")]
        if rows:
            log.info("догон %s..: пропущено транзакций: %d", wallet[:6], len(rows))
        for r in reversed(rows):   # от старых к новым
            last_sig[wallet] = r["signature"]
            spawn(handle(wallet, r["signature"], catchup=True))

    while True:
        try:
            async with session.ws_connect(cfg["rpc_ws"], heartbeat=20) as ws:
                req_wallet, sub_wallet = {}, {}
                for n, w in enumerate(cfg["targets"], 1):
                    await ws.send_json({"jsonrpc": "2.0", "id": n, "method": "logsSubscribe",
                                        "params": [{"mentions": [w]}, {"commitment": "confirmed"}]})
                    req_wallet[n] = w
                log.info("слежу за %d кошельками", len(req_wallet))
                for w in cfg["targets"]:
                    spawn(catch_up(w))
                async for msg in ws:
                    kind = getattr(msg.type, "name", "")
                    if kind in ("ERROR", "CLOSED", "CLOSING", "CLOSE"):
                        raise ConnectionError(f"WebSocket: {kind}")
                    if kind != "TEXT":
                        continue
                    d = json.loads(msg.data)
                    if "id" in d and "result" in d:
                        if d["id"] in req_wallet:
                            sub_wallet[d["result"]] = req_wallet[d["id"]]
                    elif d.get("method") == "logsNotification":
                        p = d["params"]
                        v = p["result"]["value"]
                        wallet = sub_wallet.get(p["subscription"])
                        if wallet and not v["err"]:
                            last_sig[wallet] = v["signature"]
                            spawn(handle(wallet, v["signature"]))
            log.warning("ws закрыт сервером, переподключаюсь через 3с")
        except Exception as e:  # noqa: BLE001
            log.warning("ws упал (%s), переподключаюсь через 3с", e)
        await asyncio.sleep(3)   # и после нормального закрытия тоже: иначе цикл переподключений без пауз
