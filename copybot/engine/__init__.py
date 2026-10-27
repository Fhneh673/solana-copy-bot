"""Выбор движка: быстрое ядро на Rust, если оно собрано, иначе Python."""
import os
from pathlib import Path

from .python_engine import PythonEngine
from .rust_engine import RustEngine

ROOT = Path(__file__).resolve().parents[2]


def find_binary(cfg: dict):
    exe = "copybot-core.exe" if os.name == "nt" else "copybot-core"
    for c in (cfg.get("rust_binary") or "", ROOT / "bin" / exe, ROOT / "core" / "target" / "release" / exe):
        if c and Path(c).is_file() and (os.name == "nt" or os.access(c, os.X_OK)):
            return str(c)
    return None


async def create_engine(cfg: dict, session, rpc, kp):
    """-> (engine, заметка для пользователя или None)."""
    pref = cfg["engine"]
    note = None
    if pref in ("auto", "rust"):
        binary = find_binary(cfg)
        if binary:
            eng = RustEngine(cfg, binary)
            try:
                await eng.start()
                return eng, None
            except Exception as e:  # noqa: BLE001
                await eng.stop()
                if pref == "rust":
                    raise ValueError(f"не удалось запустить ядро Rust: {e}") from None
                note = f"Ядро Rust не запустилось ({e}) — работаю на Python."
        elif pref == "rust":
            raise ValueError("ядро Rust не найдено. Соберите его: build_core.bat / build_core.sh "
                             "(нужен Rust: https://rustup.rs)")
        else:
            note = ("Быстрое ядро на Rust не найдено — работаю на Python-движке (он медленнее). "
                    "Как включить ядро — см. README, раздел «Режим Rust».")
    return PythonEngine(cfg, session, rpc, kp), note
