import os


def load_keypair():
    """Секретный ключ берём из .env / переменной PRIVATE_KEY (base58, как в Phantom)."""
    key = os.environ.get("PRIVATE_KEY", "").strip()
    if not key:
        raise ValueError("в реальном режиме нужен приватный ключ: добавьте строку "
                         "PRIVATE_KEY=... в файл .env (или заново запустите настройку).")
    try:
        from solders.keypair import Keypair
        return Keypair.from_base58_string(key)
    except ImportError:
        raise ValueError("не установлена библиотека solders (pip install -r requirements.txt)") from None
    except Exception:  # noqa: BLE001
        raise ValueError("PRIVATE_KEY не похож на ключ в формате base58. "
                         "Проверьте, что скопировали его целиком.") from None
