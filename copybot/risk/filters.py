"""Защитные фильтры токена перед покупкой (от скама и «ловушек»)."""
import asyncio
import logging
import time

from ..constants import WSOL

log = logging.getLogger(__name__)


class TokenFilters:
    def __init__(self, cfg: dict, rpc):
        self.cfg, self.f, self.rpc = cfg, cfg["filters"], rpc
        self._cache: dict[str, tuple[float, str | None]] = {}

    async def check_buy(self, engine, mint: str, spend_lamports: int, tokens_expected: int | None) -> str | None:
        """None — покупать можно; иначе строка с причиной отказа."""
        f = self.f
        if not f["enabled"] or mint in self.cfg["whitelist_mints"]:
            return None

        async def nothing():
            return None

        need_auth = f["block_mint_authority"] or f["block_freeze_authority"]
        auth, route = await asyncio.gather(
            self._authority(mint) if need_auth else nothing(),
            self._sell_check(engine, mint, spend_lamports, tokens_expected) if f["require_sell_route"] else nothing(),
            return_exceptions=True)
        for res, what in ((auth, "проверка прав токена"), (route, "проверка продажи")):
            if isinstance(res, Exception):
                # не смогли проверить — на всякий случай не покупаем
                return f"{what} не удалась: {str(res)[:100]}"
            if res:
                return res
        return None

    async def _authority(self, mint: str) -> str | None:
        hit = self._cache.get(mint)
        if hit and time.monotonic() - hit[0] < self.f["cache_sec"]:
            return hit[1]
        res = await self.rpc.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        val = res.get("value") if res else None
        if not val:
            raise RuntimeError("токен не найден в сети")
        data = val.get("data")
        info = data.get("parsed", {}).get("info") if isinstance(data, dict) else None
        if info is None:
            raise RuntimeError("не удалось прочитать данные токена")
        reason = None
        if self.f["block_mint_authority"] and info.get("mintAuthority"):
            reason = "у токена не отозван mint authority (создатель может напечатать ещё)"
        elif self.f["block_freeze_authority"] and info.get("freezeAuthority"):
            reason = "у токена есть freeze authority (создатель может заморозить ваши токены)"
        self._cache[mint] = (time.monotonic(), reason)
        return reason

    async def _sell_check(self, engine, mint: str, spend_lamports: int, tokens_expected: int | None) -> str | None:
        """Можно ли токен продать и сколько съест круг «купил-продал» по текущим котировкам."""
        amount = tokens_expected
        if not amount:
            q = await engine.quote(WSOL, mint, spend_lamports)
            amount = int(q["outAmount"])
        try:
            q = await engine.quote(mint, WSOL, int(amount))
        except Exception as e:  # noqa: BLE001
            return f"токен нельзя продать (нет маршрута): {str(e)[:80]}"
        limit = self.f["max_roundtrip_loss_pct"]
        if limit:
            loss = 1 - int(q["outAmount"]) / spend_lamports
            if loss * 100 > limit:
                return f"«купил-продал» съест {loss:.0%} (лимит {limit:g}%): токен неликвидный или нечестный"
        return None
