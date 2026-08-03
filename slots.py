"""Three-reel slot machine for the Gamesman. Pure functions, no I/O.

Symbols are weighted, so the rare ones genuinely are rare. The payout table is
tuned to sit a little under break-even — the house edge is what stops Sana Coin
inflating away, given everyone can claim a free 1,000 a day.
"""

import secrets

# (symbol, weight) — higher weight means it lands more often.
REELS = [
    ("🍒", 28),
    ("🍋", 24),
    ("🔔", 18),
    ("⭐", 13),
    ("💎", 8),
    ("7️⃣", 5),
]

# Three of a kind. Multiplies the stake.
TRIPLE = {
    "🍒": 6,
    "🍋": 8,
    "🔔": 15,
    "⭐": 35,
    "💎": 100,
    "7️⃣": 300,
}

# Two matching symbols. Cherries and lemons pair up on nearly a third of
# spins between them, so they pay nothing — otherwise the machine hands back
# more than it takes and Sana Coin becomes worthless.
PAIR = {
    "🔔": 1,
    "⭐": 2,
    "💎": 4,
    "7️⃣": 8,
}

SYMBOLS = [s for s, _ in REELS]
_WEIGHTED = [s for s, w in REELS for _ in range(w)]


def spin():
    rng = secrets.SystemRandom()
    return [rng.choice(_WEIGHTED) for _ in range(3)]


def evaluate(reels, bet):
    """Return (payout, label). Payout is the total returned, stake included."""
    a, b, c = reels
    if a == b == c:
        return bet * TRIPLE[a], f"Three {a} — {TRIPLE[a]}x"
    if a == b or b == c or a == c:
        pair = b if (a == b or b == c) else a
        mult = PAIR.get(pair)
        if mult:
            return bet * mult, f"Two {pair} — {mult}x"
        return 0, f"Two {pair}, no payout"
    return 0, "No match"


def play(bet):
    reels = spin()
    payout, label = evaluate(reels, bet)
    return {
        "reels": reels,
        "payout": payout,
        "profit": payout - bet,
        "label": label,
        "won": payout > bet,
        "pushed": payout == bet and bet > 0,
    }
