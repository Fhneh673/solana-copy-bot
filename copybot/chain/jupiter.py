import asyncio
import base64
import logging
import time

from ..errors import ConfirmTimeout, NoRoute, SkipTrade, TxFailed  # noqa: F401  (TxFailed реэкспортируется)
from .rpc import Rpc, request_timeout
from .swapresult import SwapOut

log = logging.getLogger(__name__)


class Jupiter:
    """Котировки и свопы через Jupiter. В dry_run ничего не отправляет."""

    def __init__(self, session, rpc: Rpc, cfg: dict, keypair=None):
        self.s, self.rpc, self.cfg, self.kp = session, rpc, cfg, keypair
        self.dry = cfg["dry_run"]

    def _headers(self) -> dict:
        key = self.cfg.get("jupiter_api_key")
        return {"x-api-key": key} if key else {}

    async def _get_json(self, path: str, params: dict) -> dict:
        async with self.s.get(f"{self.cfg['jupiter_url']}{path}", params=params, headers=self._headers(),
                              **request_timeout()) as r:
            self._check_status(r)
            return await r.json()

    def _check_status(self, r):
        status = getattr(r, "status", 200)
        if status in (401, 403):
            raise RuntimeError(f"Jupiter отклонил запрос (HTTP {status}): для api.jup.ag нужен бесплатный ключ "
                               "(portal.jup.ag) в JUPITER_API_KEY или jupiter_api_key")
        if status == 429:
            raise RuntimeError("Jupiter: слишком много запросов (HTTP 429), лимит тарифа исчерпан")
        if status >= 500:   # сбой на стороне Jupiter — это не «нет маршрута» и не повод считать токен мусором
            raise RuntimeError(f"Jupiter: ошибка сервера (HTTP {status}), повторю позже")

    async def quote(self, in_mint: str, out_mint: str, amount: int) -> dict:
        p = {"inputMint": in_mint, "outputMint": out_mint, "amount": str(amount),
             "slippageBps": self.cfg["slippage_bps"]}
        q = await self._get_json("/quote", p)
        if "outAmount" not in q:
            raise NoRoute(f"нет маршрута: {q}")
        return q

    async def swap(self, in_mint: str, out_mint: str, amount: int, min_out: int | None = None,
                   check_impact: bool = True) -> SwapOut:
        """Возвращает ожидаемый outAmount (по котировке) + .sig. min_out — защита от «цена уже убежала».

        check_impact=True только для покупок: продажа (стоп-лосс, /close) не должна блокироваться
        лимитом price impact — выйти из позиции важнее."""
        q = await self.quote(in_mint, out_mint, amount)
        out = int(q["outAmount"])
        impact = float(q.get("priceImpactPct") or 0) * 100
        if check_impact and impact > self.cfg["max_price_impact_pct"]:
            raise SkipTrade(f"price impact {impact:.2f}% > лимита {self.cfg['max_price_impact_pct']:g}%")
        if min_out and out < min_out:
            raise SkipTrade(f"цена уже хуже цены цели (получим {out}, нужно не меньше {min_out})")
        if self.dry:
            log.info("[DRY] swap %s -> %s in=%s out=%s impact=%.2f%%",
                     in_mint[:6], out_mint[:6], amount, out, impact)
            return SwapOut.make(out)

        from solders.transaction import VersionedTransaction
        jito = self.cfg.get("jito", {})
        use_jito = bool(jito.get("enabled"))
        fee = ({"jitoTipLamports": jito["tip_lamports"]} if use_jito else
               {"priorityLevelWithMaxLamports": {
                   "priorityLevel": "high", "maxLamports": self.cfg["max_priority_fee_lamports"]}})
        body = {"quoteResponse": q, "userPublicKey": str(self.kp.pubkey()),
                "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": fee}
        async with self.s.post(f"{self.cfg['jupiter_url']}/swap", json=body, headers=self._headers(),
                               **request_timeout()) as r:
            self._check_status(r)
            sw = await r.json()
        if "swapTransaction" not in sw:
            raise RuntimeError(f"Jupiter не вернул транзакцию: {sw}")
        raw = VersionedTransaction.from_bytes(base64.b64decode(sw["swapTransaction"]))
        signed = VersionedTransaction(raw.message, [self.kp])
        b64 = base64.b64encode(bytes(signed)).decode()
        sig = await self.rpc.call("sendTransaction", [
            b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}])
        if use_jito:
            try:
                await self.rpc.call_url(jito["url"], "sendTransaction", [b64, {"encoding": "base64"}])
            except Exception as e:  # noqa: BLE001
                log.warning("Jito: %s", e)
        log.info("отправлено: https://solscan.io/tx/%s", sig)
        await self._confirm(sig, out)
        return SwapOut.make(out, sig)

    async def _confirm(self, sig: str, out: int | None = None):
        timeout = self.cfg.get("confirm_timeout_sec", 30)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                res = await self.rpc.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}])
                st = res["value"][0]
                if st:
                    if st.get("err"):
                        raise TxFailed(f"транзакция не прошла в сети: {st['err']}")
                    if st.get("confirmationStatus") in ("confirmed", "finalized"):
                        return
            except TxFailed:
                raise
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.4)
        # Транзакция могла пройти позже: вызывающий код обязан сверить баланс, а не считать покупку неудачной.
        raise ConfirmTimeout(f"транзакция не подтвердилась за {timeout} с ({sig})", sig, out)
