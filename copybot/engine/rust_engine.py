"""Быстрое ядро на Rust. Запускается как дочерний процесс, общение — JSON-строками через stdin/stdout.

Rust делает всё, где важна скорость: WebSocket-слежение, разбор сделки цели, котировки Jupiter,
подпись и отправку транзакции, ожидание подтверждения. Python принимает решения (размер, лимиты,
SL/TP, просадка) и хранит состояние.
"""
import asyncio
import json
import logging
import os

from ..chain.swapresult import SwapOut
from ..errors import ConfirmTimeout, NoRoute, SkipTrade

log = logging.getLogger("copybot.rust")


class RustEngine:
    name = "rust"

    def __init__(self, cfg: dict, cmd):
        self.cfg = cfg
        self.cmd = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
        self.proc = None
        self.pubkey = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events: asyncio.Queue = asyncio.Queue()
        self._ready: asyncio.Future | None = None
        self._tasks: set[asyncio.Task] = set()
        self._stderr_tail: list[str] = []

    # ------------------------------------------------------------ запуск / остановка
    def _init_message(self) -> dict:
        c = self.cfg
        return {
            "cmd": "init",
            "rpc_http": c["rpc_http"], "rpc_ws": c["rpc_ws"], "targets": c["targets"],
            "jupiter_url": c["jupiter_url"], "jupiter_api_key": c.get("jupiter_api_key") or "",
            "slippage_bps": c["slippage_bps"],
            "max_price_impact_pct": c["max_price_impact_pct"],
            "max_priority_fee_lamports": c["max_priority_fee_lamports"],
            "dry_run": c["dry_run"], "send_rpcs": self._send_rpcs(),
            "jito_tip_lamports": c["jito"]["tip_lamports"] if c["jito"]["enabled"] else 0,
            "confirm_timeout_sec": c.get("confirm_timeout_sec", 30),
            "warmup_sec": c.get("warmup_sec", 20),
        }

    def _send_rpcs(self) -> list:
        urls = list(self.cfg.get("send_rpcs", []))
        if self.cfg["jito"]["enabled"]:
            urls.append(self.cfg["jito"]["url"])  # Jito получает ту же транзакцию параллельно
        return urls

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def start(self, timeout: float = 20):
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self.proc = await asyncio.create_subprocess_exec(
            *self.cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=os.environ.copy())  # PRIVATE_KEY уйдёт через окружение
        self._spawn(self._read_stdout())
        self._spawn(self._read_stderr())
        await self._send(self._init_message())
        try:
            ready = await asyncio.wait_for(self._ready, timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(f"ядро не ответило за {timeout:g} с") from None
        self.pubkey = ready.get("pubkey")
        log.info("ядро Rust запущено (v%s)", ready.get("version"))

    async def stop(self):
        if not self.proc or self.proc.returncode is not None:
            return
        try:
            await self._send({"cmd": "shutdown"})
            await asyncio.wait_for(self.proc.wait(), 2)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass

    # ------------------------------------------------------------ обмен сообщениями
    async def _send(self, msg: dict):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def _read_stdout(self):
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                t = m.get("t")
                if t == "ready":
                    if self._ready and not self._ready.done():
                        self._ready.set_result(m)
                elif t == "res":
                    fut = self._pending.get(m.get("id"))
                    if fut and not fut.done():
                        fut.set_result(m)
                elif t == "swap":
                    self._events.put_nowait(m)
                elif t == "log":
                    getattr(log, {"warn": "warning"}.get(m.get("level"), m.get("level", "info")),
                            log.info)(m.get("msg", ""))
        finally:
            await asyncio.sleep(0.2)  # дать дочитать stderr
            err = RuntimeError("ядро Rust завершилось" + (f": {self._stderr_tail[-1]}" if self._stderr_tail else ""))
            if self._ready and not self._ready.done():
                self._ready.set_exception(err)
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(err)
            self._events.put_nowait(None)

    async def _read_stderr(self):
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            text = line.decode(errors="replace").strip()
            if text:
                self._stderr_tail = (self._stderr_tail + [text])[-5:]
                log.warning("[rust stderr] %s", text)

    async def _call(self, msg: dict, timeout: float) -> dict:
        if self.proc is None or self.proc.returncode is not None:
            raise RuntimeError("ядро Rust не запущено")
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({**msg, "id": rid})
            res = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("ядро Rust не ответило вовремя") from None
        finally:
            self._pending.pop(rid, None)
        if not res.get("ok"):
            err = res.get("err", "ошибка ядра")
            if res.get("unconfirmed"):
                raise ConfirmTimeout(err, res.get("sig"), int(res["out"]) if res.get("out") else None)
            if res.get("skip"):
                raise SkipTrade(err)
            if res.get("no_route") or err.startswith("нет маршрута"):
                raise NoRoute(err)
            raise RuntimeError(err)
        return res

    # ------------------------------------------------------------ тот же интерфейс, что у PythonEngine
    async def quote(self, in_mint: str, out_mint: str, amount: int) -> dict:
        res = await self._call({"cmd": "quote", "in": in_mint, "out": out_mint, "amount": str(amount)}, 15)
        return {"outAmount": res["out"], "priceImpactPct": res.get("impact") or 0}

    async def swap(self, in_mint: str, out_mint: str, amount: int, min_out: int | None = None,
                   check_impact: bool = True):
        timeout = self.cfg.get("confirm_timeout_sec", 30) + 20
        res = await self._call({"cmd": "swap", "in": in_mint, "out": out_mint, "amount": str(amount),
                                "min_out": str(min_out) if min_out else "",
                                "check_impact": bool(check_impact)}, timeout)
        log.info("исполнено ядром за %s мс%s", res.get("ms"),
                 f" · https://solscan.io/tx/{res['sig']}" if res.get("sig") else "")
        return SwapOut.make(int(res["out"]), res.get("sig"))

    async def run(self, trader):
        """Получает сделки целей от ядра и отдаёт их торговой логике."""
        while True:
            m = await self._events.get()
            if m is None:
                raise RuntimeError("ядро Rust остановилось")
            t = {"side": m["side"], "mint": m["mint"], "sol": m["sol"], "frac": m.get("frac"),
                 "tokens": int(m["tokens"]) if m.get("tokens") else None}
            log.info("цель %s..: %s %s на %.3f SOL (ядро заметило за %s мс)",
                     m["wallet"][:6], t["side"], t["mint"][:6], t["sol"], m.get("detect_ms"))
            self._spawn(self._handle(trader, m["wallet"], t))

    @staticmethod
    async def _handle(trader, wallet: str, t: dict):
        try:
            await trader.copy(wallet, t)
        except Exception as e:  # noqa: BLE001
            log.error("copy failed: %s", e)
