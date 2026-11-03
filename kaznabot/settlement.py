"""Взаиморасчёт по долям: каждая позиция чека делится поровну на своих участников.

Если все позиции делятся на всех, это ровно «каждый должен потратить среднее».
"""

from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Entry[K: Hashable]:
    payer: K
    amount: int  # копейки
    participants: tuple[K, ...]
    salt: int = 0  # сдвиг, кому достанутся неделимые копейки


@dataclass(frozen=True)
class Transfer[K: Hashable]:
    debtor: K
    creditor: K
    amount: int  # копейки


def split_amount[K: Hashable](amount: int, participants: Sequence[K], salt: int = 0) -> dict[K, int]:
    """Делит сумму поровну; остаток раздаётся по копейке, начиная с участника под номером salt."""
    n = len(participants)
    base, remainder = divmod(amount, n)
    shares = dict.fromkeys(participants, base)
    for i in range(remainder):
        shares[participants[(salt + i) % n]] += 1
    return shares


def totals[K: Hashable](entries: Iterable[Entry[K]]) -> tuple[dict[K, int], dict[K, int]]:
    """(сколько каждый заплатил, какова его доля). Сумма долей всегда равна сумме оплат."""
    paid: dict[K, int] = {}
    owed: dict[K, int] = {}
    for e in entries:
        paid[e.payer] = paid.get(e.payer, 0) + e.amount
        for person, share in split_amount(e.amount, e.participants or (e.payer,), e.salt).items():
            owed[person] = owed.get(person, 0) + share
    return paid, owed


def compute_balances[K: Hashable](entries: Iterable[Entry[K]]) -> dict[K, int]:
    """Положительный баланс — ему должны, отрицательный — он должен."""
    paid, owed = totals(entries)
    return {p: paid.get(p, 0) - owed.get(p, 0) for p in dict.fromkeys([*paid, *owed])}


def settle[K: Hashable](balances: dict[K, int]) -> list[Transfer[K]]:
    """Жадно сводит самого большого должника с самым большим кредитором (≤ n−1 переводов)."""
    debtors = sorted(((-b, p) for p, b in balances.items() if b < 0), key=lambda x: -x[0])
    creditors = sorted(((b, p) for p, b in balances.items() if b > 0), key=lambda x: -x[0])
    transfers: list[Transfer[K]] = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        debt, debtor = debtors[i]
        credit, creditor = creditors[j]
        amount = min(debt, credit)
        transfers.append(Transfer(debtor, creditor, amount))
        debtors[i] = (debt - amount, debtor)
        creditors[j] = (credit - amount, creditor)
        if debtors[i][0] == 0:
            i += 1
        if creditors[j][0] == 0:
            j += 1
    return transfers
