"""European roulette for the Gamesman. Pure functions, no I/O.

Single zero — 37 pockets rather than the American 38 — because the extra green
pocket doubles the house edge and there is no reason to be that unkind. Odds
are the real ones: a straight number pays 35 to 1, the even-money bets pay 1
to 1, and dozens and columns pay 2 to 1. Zero loses every outside bet, which
is where the house edge lives.
"""

import secrets

POCKETS = 37                      # 0–36

RED = frozenset({1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36})


def colour_of(number):
    if number == 0:
        return "green"
    return "red" if number in RED else "black"


# Each bet knows how to spot a winner and what it pays. `pays` is the "to one"
# figure, so a winning stake comes back multiplied by pays + 1.
BETS = {
    "red":    {"label": "Red",           "pays": 1, "hits": lambda n: colour_of(n) == "red"},
    "black":  {"label": "Black",         "pays": 1, "hits": lambda n: colour_of(n) == "black"},
    "odd":    {"label": "Odd",           "pays": 1, "hits": lambda n: n != 0 and n % 2 == 1},
    "even":   {"label": "Even",          "pays": 1, "hits": lambda n: n != 0 and n % 2 == 0},
    "low":    {"label": "1 to 18",       "pays": 1, "hits": lambda n: 1 <= n <= 18},
    "high":   {"label": "19 to 36",      "pays": 1, "hits": lambda n: 19 <= n <= 36},
    "dozen1": {"label": "1st dozen",     "pays": 2, "hits": lambda n: 1 <= n <= 12},
    "dozen2": {"label": "2nd dozen",     "pays": 2, "hits": lambda n: 13 <= n <= 24},
    "dozen3": {"label": "3rd dozen",     "pays": 2, "hits": lambda n: 25 <= n <= 36},
    "col1":   {"label": "1st column",    "pays": 2, "hits": lambda n: n != 0 and n % 3 == 1},
    "col2":   {"label": "2nd column",    "pays": 2, "hits": lambda n: n != 0 and n % 3 == 2},
    "col3":   {"label": "3rd column",    "pays": 2, "hits": lambda n: n != 0 and n % 3 == 0},
    # Filled in by `describe` and `play`, since the winner depends on the
    # number the player picked rather than on a fixed rule.
    "number": {"label": "Straight up",   "pays": 35, "hits": None},
}


def valid(kind, number=None):
    if kind not in BETS:
        return False
    if kind == "number":
        return isinstance(number, int) and 0 <= number < POCKETS
    return True


def describe(kind, number=None):
    if kind == "number":
        return f"Straight up on {number}"
    return BETS[kind]["label"]


def spin():
    """One pocket, from the OS random source."""
    return secrets.SystemRandom().randrange(POCKETS)


def play(kind, stake, number=None, landed=None):
    """Settle one bet. `landed` is only for tests that need a fixed wheel."""
    if landed is None:
        landed = spin()
    pays = BETS[kind]["pays"]
    if kind == "number":
        won = landed == number
    else:
        won = BETS[kind]["hits"](landed)

    payout = stake * (pays + 1) if won else 0
    return {
        "number": landed,
        "colour": colour_of(landed),
        "bet": describe(kind, number),
        "kind": kind,
        "pick": number,
        "won": won,
        "pays": pays,
        "payout": payout,
        "profit": payout - stake,
        "label": _label(won, landed, pays),
    }


def _label(won, landed, pays):
    where = f"{landed} {colour_of(landed)}"
    if won:
        return f"{where} — paid {pays} to 1."
    if landed == 0:
        return "Zero. The house takes everything outside."
    return f"{where}. Not this time."
