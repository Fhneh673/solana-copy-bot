"""Python-вотчер: подписка, дедупликация, догон после обрыва. python tests/test_watcher.py  или  pytest tests/"""
import asyncio
import json
import time

import _helpers  # noqa: F401
from copybot.chain import watcher

W1, W2 = "WALLET1" + "1" * 30, "WALLET2" + "2" * 30
MINT = "MINTXYZ"


class Msg:
    def __init__(self, data=None, kind="TEXT"):
        self.data = json.dumps(data) if data is not None else ""
        self.type = type("T", (), {"name": kind})()


class WS:
    def __init__(self, msgs):
        self.msgs, self.sent = msgs, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send_json(self, m):
        self.sent.append(m)

    def __aiter__(self):
        async def gen():
            for m in self.msgs:
                yield m
        return gen()


class Session:
    def __init__(self, sessions):
        self.sessions, self.n = sessions, 0

    def ws_connect(self, url, heartbeat=None):
        ws = self.sessions[min(self.n, len(self.sessions) - 1)]
        self.n += 1
        return ws


def buy_tx(wallet, age=1):
    return {"blockTime": int(time.time()) - age,
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet}]}},
            "meta": {"err": None, "fee": 5000, "preBalances": [2_000_000_000], "postBalances": [999_995_000],
                     "preTokenBalances": [], "postTokenBalances": [
                         {"mint": MINT, "owner": wallet, "uiTokenAmount": {"amount": "500"}}]}}


class Rpc:
    def __init__(self, txs, missed):
        self.txs, self.missed, self.calls = txs, missed, []

    async def get_tx(self, sig, tries=6):
        return self.txs.get(sig)

    async def call(self, method, params):
        self.calls.append((method, params))
        return self.missed


class Trader:
    def __init__(self):
        self.got = []

    async def copy(self, wallet, t):
        self.got.append((wallet, t["side"], t["mint"]))


def notif(sub, sig, err=None):
    return Msg({"method": "logsNotification", "params": {"subscription": sub, "result": {"value": {
        "err": err, "signature": sig}}}})


def test_subscribe_dedupe_and_catchup(monkeypatch=None):
    async def go():
        # соединение 1: обе цели подписаны; сделка s1 у W1 (дважды — дубль), сообщение с ошибкой и «чужая» подписка
        ws1 = WS([Msg({"id": 1, "result": 11}), Msg({"id": 2, "result": 22}),
                  notif(11, "s1"), notif(11, "s1"), notif(11, "bad", err={"x": 1}), notif(99, "zzz"),
                  Msg(kind="ERROR")])
        ws2 = WS([Msg({"id": 1, "result": 31}), Msg({"id": 2, "result": 32})])   # после обрыва
        # s1 — обычная покупка, old — старая покупка (пропущена во время обрыва), fresh — свежая
        rpc = Rpc({"s1": buy_tx(W1), "old": buy_tx(W1, age=500), "fresh": buy_tx(W1, age=5)},
                  [{"signature": "fresh", "err": None}, {"signature": "old", "err": None},
                   {"signature": "failed", "err": {"e": 1}}])
        tr = Trader()
        sleeps = []
        real_sleep = asyncio.sleep

        async def fast_sleep(d, *a):
            sleeps.append(d)
            await real_sleep(0)
        watcher.asyncio.sleep = fast_sleep
        cfg = {"rpc_ws": "ws://x", "targets": [W1, W2]}
        task = asyncio.create_task(watcher.watch(cfg, Session([ws1, ws2]), rpc, tr))
        for _ in range(50):
            await real_sleep(0.01)
        task.cancel()
        watcher.asyncio.sleep = real_sleep
        assert ws1.sent[0]["params"][0] == {"mentions": [W1]} and len(ws1.sent) == 2
        assert tr.got.count((W1, "buy", MINT)) == 2, tr.got    # s1 один раз (дубль отброшен) + fresh при догоне; old пропущен
        assert any(m == "getSignaturesForAddress" and p[1]["until"] == "s1" for m, p in rpc.calls)
        assert 3 in sleeps                                      # пауза перед переподключением есть
    asyncio.run(go())


def test_dedup_survives_cache_overflow():
    """Раньше при переполнении кэш очищался целиком, и только что обработанная сделка могла скопироваться второй раз."""
    async def go():
        n = 5001                                    # лимит кэша 5000: на последнем сообщении происходит вытеснение
        tx = buy_tx(W1)
        rpc = Rpc({f"n{i}": tx for i in range(n)}, [])
        msgs = [Msg({"id": 1, "result": 11}), Msg({"id": 2, "result": 22})]
        msgs += [notif(11, f"n{i}") for i in range(n)]
        msgs += [notif(11, f"n{n - 1}")]            # дубль самой свежей сделки
        tr = Trader()
        cfg = {"rpc_ws": "ws://x", "targets": [W1, W2]}
        task = asyncio.create_task(watcher.watch(cfg, Session([WS(msgs)]), rpc, tr))
        for _ in range(500):
            await asyncio.sleep(0.01)
            if len(tr.got) >= n:
                break
        await asyncio.sleep(0.2)                    # дать шанс «лишней» копии появиться
        task.cancel()
        assert len(tr.got) == n, len(tr.got)
    asyncio.run(go())


if __name__ == "__main__":
    test_subscribe_dedupe_and_catchup()
    test_dedup_survives_cache_overflow()
    print("OK: watcher")
