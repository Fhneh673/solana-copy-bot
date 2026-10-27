"""Запасной движок на чистом Python (работает всегда, но медленнее ядра Rust)."""
from ..chain.jupiter import Jupiter
from ..chain.watcher import watch


class PythonEngine:
    name = "python"
    pubkey = None

    def __init__(self, cfg, session, rpc, kp=None):
        self.cfg, self.session, self.rpc = cfg, session, rpc
        self.jup = Jupiter(session, rpc, cfg, kp)

    async def start(self):
        return None

    async def quote(self, in_mint: str, out_mint: str, amount: int) -> dict:
        return await self.jup.quote(in_mint, out_mint, amount)

    async def swap(self, in_mint: str, out_mint: str, amount: int, min_out: int | None = None,
                   check_impact: bool = True):
        return await self.jup.swap(in_mint, out_mint, amount, min_out=min_out, check_impact=check_impact)

    async def run(self, trader):
        await watch(self.cfg, self.session, self.rpc, trader)

    async def stop(self):
        return None
