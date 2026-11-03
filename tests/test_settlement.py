from kaznabot.money import fmt_rub, rub_to_kop
from kaznabot.settlement import Entry, Transfer, compute_balances, settle, split_amount, totals

ALL = ("vasya", "petya", "kolya")


def test_everything_shared_equals_average() -> None:
    # Вася 3000, Петя 2000, Коля 0 → каждый должен 1666,67
    balances = compute_balances([Entry("vasya", 300000, ALL), Entry("petya", 200000, ALL, salt=1)])
    assert sum(balances.values()) == 0
    assert balances == {"vasya": 133334, "petya": 33333, "kolya": -166667}
    assert settle(balances) == [Transfer("kolya", "vasya", 133334), Transfer("kolya", "petya", 33333)]


def test_item_only_for_some_people() -> None:
    # Хлеб 90 на всех, пиво 200 только Васе и Пете, платил Коля
    paid, owed = totals([Entry("kolya", 9000, ALL), Entry("kolya", 20000, ("vasya", "petya"))])
    assert paid == {"kolya": 29000}
    assert owed == {"vasya": 13000, "petya": 13000, "kolya": 3000}
    assert settle(compute_balances([Entry("kolya", 9000, ALL), Entry("kolya", 20000, ("vasya", "petya"))])) == [
        Transfer("vasya", "kolya", 13000),
        Transfer("petya", "kolya", 13000),
    ]


def test_personal_item_does_not_affect_others() -> None:
    assert compute_balances([Entry("vasya", 50000, ("vasya",))]) == {"vasya": 0}


def test_split_remainder_rotates_by_salt() -> None:
    assert split_amount(100, ["a", "b", "c"]) == {"a": 34, "b": 33, "c": 33}
    assert split_amount(100, ["a", "b", "c"], salt=1) == {"a": 33, "b": 34, "c": 33}
    assert split_amount(101, ["a", "b", "c"], salt=2) == {"a": 34, "b": 33, "c": 34}


def test_transfers_close_all_balances() -> None:
    entries = [
        Entry(p, a, ("a", "b", "c", "d", "e"), salt=i)
        for i, (p, a) in enumerate([("a", 123457), ("c", 98765), ("d", 1), ("e", 50000)])
    ]
    entries.append(Entry("b", 777, ("b", "c")))
    balances = compute_balances(entries)
    assert sum(balances.values()) == 0
    transfers = settle(balances)
    assert len(transfers) <= len(balances) - 1
    left = dict(balances)
    for t in transfers:
        assert t.amount > 0
        left[t.debtor] += t.amount
        left[t.creditor] -= t.amount
    assert all(v == 0 for v in left.values())


def test_money_helpers() -> None:
    assert rub_to_kop(179.98) == 17998
    assert rub_to_kop("0.005") == 1
    assert fmt_rub(123456) == "1 234,56 ₽"
    assert fmt_rub(100000) == "1 000 ₽"
    assert fmt_rub(-500, signed=True) == "−5 ₽"
    assert fmt_rub(500, signed=True) == "+5 ₽"
