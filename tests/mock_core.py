"""Имитация ядра Rust: тот же JSON-протокол. Нужна, чтобы проверять Python-часть без компиляции Rust."""
import json
import sys
import threading
import time

WSOL = "So11111111111111111111111111111111111111112"


def emit(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()


def scenario():
    time.sleep(0.4)
    emit({"t": "swap", "wallet": "TARGETWALLET1111", "sig": "s1", "side": "buy",
          "mint": "MINTAAAAAA", "sol": 1.0, "frac": None, "tokens": "1000000000000", "detect_ms": 42})
    time.sleep(0.6)
    emit({"t": "swap", "wallet": "TARGETWALLET1111", "sig": "s2", "side": "sell",
          "mint": "MINTAAAAAA", "sol": 1.2, "frac": 1.0, "tokens": "1000000", "detect_ms": 37})


init = json.loads(sys.stdin.readline())
emit({"t": "ready", "version": "mock", "pubkey": None})
emit({"t": "log", "level": "info", "msg": "mock: init ok, targets=%d" % len(init["targets"])})
threading.Thread(target=scenario, daemon=True).start()

for line in sys.stdin:
    c = json.loads(line)
    if c["cmd"] == "shutdown":
        break
    if c["cmd"] in ("quote", "swap"):
        amt = int(c["amount"])
        if "FAIL" in (c["in"], c["out"]):
            emit({"t": "res", "id": c["id"], "ok": False, "err": "нет маршрута: mock"})
            continue
        out = amt * 1000 if c["in"] == WSOL else amt // 1000
        if c["cmd"] == "swap" and c.get("min_out") and out < int(c["min_out"]):
            emit({"t": "res", "id": c["id"], "ok": False, "err": "цена уже хуже цены цели (mock)"})
            continue
        r = {"t": "res", "id": c["id"], "ok": True, "out": str(out), "impact": "0.001"}
        if c["cmd"] == "swap":
            r.update(sig="mocksig", ms=5)
        emit(r)
