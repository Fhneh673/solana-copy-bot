class SwapOut(int):
    """Сколько получили (int) + подпись транзакции в .sig, если она была."""
    sig = None

    @classmethod
    def make(cls, out, sig=None):
        o = cls(out)
        o.sig = sig
        return o
