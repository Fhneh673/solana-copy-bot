**English** | [Русский](README.ru.md)

# Solana Copy Bot

A copy-trading bot for Solana. It watches the wallets you choose, and when one of them buys or sells a token it repeats the trade from your own wallet, with your sizing and your risk limits.

Hybrid design: a **Rust core** handles everything latency-sensitive (WebSocket watching, swap parsing, Jupiter quotes, signing, sending, confirmation), while **Python** makes the decisions (position sizing, limits, stop-loss, take-profit, drawdown protection, state, Telegram control). If Rust isn't installed or the core crashes, the bot falls back to a pure-Python engine automatically.

> **Disclaimer.** This is a software project, not financial advice. Copy trading loses money: copies are always delayed, prices move, and many tokens are scams. Run it in the default **dry-run mode** first, then use a separate wallet with a small amount. Provided "as is", without warranty (see [LICENSE](LICENSE)).

## Features

**Copying**
- Multiple target wallets, each with its own name and overrides (`ratio`, `fixed_sol`, `max_sol_per_trade`, `enabled`).
- Sizing modes: ratio of the target's trade, fixed SOL amount, or percent of balance.
- Mirrors sells proportionally; by default only the wallet that opened a position can close it.

**Entry protection** (on by default)
- Rejects tokens with an active mint authority or freeze authority.
- Checks that the token is sellable and that a simulated round trip doesn't lose more than a set percentage.
- Skips buys when the current price is already much worse than the target's price.

**Exit and risk management**
- Stop-loss, laddered take-profit, trailing stop, max holding time.
- Per-trade, per-position and open-position limits, SOL reserve for fees.
- Portfolio drawdown limit that halts new buys (or closes everything).
- Per-wallet statistics, with automatic pause for target wallets that lose money.

**Reliability**
- Waits for on-chain confirmation; unconfirmed buys are tracked as `pending` and reconciled later.
- Results are computed from actual wallet balance changes (fees and tips included), not from quotes.
- Catches up on missed target trades after a WebSocket reconnect.
- State persisted in `state.json`; every trade appended to `trades.csv`.
- Strict config validation: typos in settings stop startup with a hint.

**Operations**
- Interactive setup wizard, pre-flight connectivity checks (`check`).
- Optional Telegram notifications and commands (`/status`, `/positions`, `/wallets`, `/pause`, `/resume`, `/close`, `/closeall`, ...), restricted to your chat and user IDs.
- Secrets live only in `.env`; the private key is passed to the Rust core through the environment, never over the IPC channel, and is masked in logs.

## Architecture

```
main.py                 entry point (+ --check)
setup_wizard.py         interactive configuration
copybot/
  trader.py             orchestrator: copy / close
  engine/               rust_engine.py (core subprocess, JSON over stdin/stdout)
                        python_engine.py (fallback)
  chain/                rpc.py, jupiter.py, watcher.py, wallet.py
  strategy/             parser.py (swap parsing), sizing.py
  risk/                 limits.py, monitor.py (SL / TP / drawdown), filters.py
  storage/              state.py (state.json), journal.py (trades.csv)
  notify/               telegram.py
  control.py            runtime commands
core/                   Rust core (watcher, parser, exec, wallet, rpc)
tests/                  offline tests with a mock core and fake RPC
```

The Python-to-core protocol is line-delimited JSON. Python to core: `init`, `quote`, `swap`, `shutdown`. Core to Python: `ready`, `swap` (a target trade), `res` (command response), `log`.

## Quick start

Requirements: Python 3.10+. Rust is optional (faster execution).

```bash
git clone <this repo>
cd copy_bot
bash start.sh          # Windows: start.bat
```

On first run the script creates a virtualenv, installs dependencies, builds the Rust core if `cargo` is available, and starts the setup wizard. Other commands:

| Action | Mac / Linux | Windows |
|---|---|---|
| Run | `bash start.sh` | `start.bat` |
| Reconfigure | `bash start.sh setup` | `start.bat setup` |
| Check connections only | `bash start.sh check` | `start.bat check` |
| Rebuild Rust core | `bash start.sh build` | `start.bat build` |

You will need an RPC endpoint with WebSocket support (a free [Helius](https://www.helius.dev) key works), the wallet addresses to follow, and (recommended) a free Jupiter API key from [portal.jup.ag](https://portal.jup.ag); without one the bot falls back to the deprecated lite-api endpoint. For live mode, also a **dedicated** wallet's private key (base58); it is stored only in your local `.env`.

See [`config.example.json`](config.example.json) for every setting. The full settings reference is in the [Russian README](README.ru.md).

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/
cd core && cargo test      # Rust core: swap parsing and signing
```

Tests run offline using a mock core and a fake RPC. They cover sizing, filters, risk limits, stop-loss / take-profit, drawdown halting, pending-transaction reconciliation, state round-trips, the Jupiter client, the watcher, config validation and Telegram access control.

## Known limitations

- Understands plain SOL <-> token swaps; stablecoin routes and complex multi-hop trades are skipped.
- Stop-loss and take-profit are polled every few seconds, so price can gap past them on fast-moving tokens.
- The optional Jito bundle support has not been verified on mainnet; test it with minimal amounts.
- Exits can fail if a token loses all liquidity.
- If a *sell* is sent but its confirmation times out, the position's recorded size is not corrected until the next successful close. Until then its valuation can be overstated. Buys do not have this problem (they go through the `pending` reconciliation).
- The trading path has been tested offline with mocks (tests above), not with real funds on mainnet. Treat live mode as experimental and start with tiny amounts.

## License

[MIT](LICENSE)
