"""Деньги храним в целых копейках, чтобы не ловить ошибки округления float."""

from decimal import ROUND_HALF_UP, Decimal


def rub_to_kop(value: float | int | str | Decimal) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_rub(kop: int, signed: bool = False) -> str:
    sign = "−" if kop < 0 else ("+" if signed and kop > 0 else "")
    rub, k = divmod(abs(kop), 100)
    rub_str = f"{rub:,}".replace(",", " ")
    return f"{sign}{rub_str},{k:02d} ₽" if k else f"{sign}{rub_str} ₽"
