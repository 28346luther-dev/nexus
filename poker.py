"""Simplified Texas Hold'em for the Gamesman. Pure functions, no I/O.

Simplified means there is no raising: each betting round costs a fixed amount
to stay in, so every decision is just "stay" or "fold". That keeps a table of
six people moving without anyone needing to understand pot odds.

Shape of a hand:

    join   — each player pays the ante into the pot
    deal   — two hole cards each, and the flop, then a betting round
    turn   — fourth community card, betting round
    river  — fifth community card, betting round
    showdown — best five cards from the seven wins the pot

Hands are ranked with the standard categories; ties split the pot.
"""

import secrets
from itertools import combinations

SUITS = ("♠", "♥", "♦", "♣")
RANKS = ("2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A")
RANK_VALUE = {r: i + 2 for i, r in enumerate(RANKS)}

MIN_PLAYERS = 2
MAX_PLAYERS = 8

# Betting rounds, in order. 'showdown' is not a betting round.
STAGES = ("flop", "turn", "river", "showdown")

CATEGORY_NAMES = {
    8: "Straight flush",
    7: "Four of a kind",
    6: "Full house",
    5: "Flush",
    4: "Straight",
    3: "Three of a kind",
    2: "Two pair",
    1: "Pair",
    0: "High card",
}


def new_deck():
    deck = [f"{rank}{suit}" for suit in SUITS for rank in RANKS]
    secrets.SystemRandom().shuffle(deck)
    return deck


def rank_of(card):
    return card[:-1]


def suit_of(card):
    return card[-1]


# ------------------------------------------------------------- hand ranking

def _straight_high(values):
    """Highest card of a straight in `values`, or None.

    Ace plays low in the 5-4-3-2-A wheel, which is the one case where the ace
    is not the top card.
    """
    unique = set(values)
    if {14, 2, 3, 4, 5} <= unique:
        best = 5
    else:
        best = None
    for high in range(14, 5, -1):
        if {high, high - 1, high - 2, high - 3, high - 4} <= unique:
            return high
    return best


def score5(cards):
    """(category, tiebreakers) for exactly five cards. Higher compares better."""
    values = sorted((RANK_VALUE[rank_of(c)] for c in cards), reverse=True)
    suits = [suit_of(c) for c in cards]
    flush = len(set(suits)) == 1
    straight = _straight_high(values)

    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    # Sort by how many of a rank first, then by the rank itself.
    grouped = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    shape = [n for _, n in grouped]
    ordered = [v for v, _ in grouped]

    if flush and straight:
        return (8, (straight,))
    if shape[0] == 4:
        return (7, tuple(ordered))
    if shape[:2] == [3, 2]:
        return (6, tuple(ordered))
    if flush:
        return (5, tuple(values))
    if straight:
        return (4, (straight,))
    if shape[0] == 3:
        return (3, tuple(ordered))
    if shape[:2] == [2, 2]:
        return (2, tuple(ordered))
    if shape[0] == 2:
        return (1, tuple(ordered))
    return (0, tuple(values))


def best_hand(cards):
    """Best five-card score from five or more cards, with the cards used."""
    best, best_cards = None, None
    for combo in combinations(cards, 5):
        s = score5(combo)
        if best is None or s > best:
            best, best_cards = s, combo
    return {
        "score": best,
        "cards": list(best_cards),
        "name": CATEGORY_NAMES[best[0]],
    }


def compare(a, b):
    """1 if hand a wins, -1 if b wins, 0 for a tie."""
    if a["score"] > b["score"]:
        return 1
    if a["score"] < b["score"]:
        return -1
    return 0


# ---------------------------------------------------------------- lifecycle

def new_table(host_id, ante):
    return {
        "kind": "poker",
        "stage": "waiting",
        "deck": [],
        "board": [],
        "pot": 0,
        "ante": ante,
        "players": [new_player(host_id)],
        "result": None,
    }


def new_player(user_id, cpu=False, name=None):
    return {
        "userId": user_id,
        "cpu": cpu,
        "name": name,
        "hole": [],
        "folded": False,
        "acted": False,
        "contributed": 0,
    }


# House players sit on negative ids so they can never collide with a real one.
CPU_NAMES = ("Ada", "Rex", "Sana", "Bishop", "Marlow", "Odds", "Kit")


def add_cpu(state):
    used = {p["userId"] for p in state["players"]}
    index = 0
    while -(index + 1) in used:
        index += 1
    player = new_player(-(index + 1), cpu=True, name=CPU_NAMES[index % len(CPU_NAMES)])
    player["contributed"] = state["ante"]
    state["pot"] += state["ante"]
    state["players"].append(player)
    return player


def cpu_decision(state, player):
    """Rough but not daft: play made hands, chase less as the board fills."""
    rng = secrets.SystemRandom()
    category = best_hand(player["hole"] + state["board"])["score"][0]
    if category >= 2:                    # two pair or better
        return "stay"
    if category == 1:                    # a pair is usually worth one more card
        return "stay" if rng.random() < 0.85 else "fold"
    # Nothing yet — keep looking early, give up late.
    chase = {"flop": 0.55, "turn": 0.35, "river": 0.18}
    return "stay" if rng.random() < chase.get(state["stage"], 0.3) else "fold"


def play_cpus(state):
    """Let the house players take their turns, advancing streets as they go.

    Stops as soon as a human still owes an action, so it never plays past
    someone's decision.
    """
    for _ in range(len(STAGES) * 2):
        if state["stage"] in ("waiting", "showdown"):
            break
        pending = [p for p in active(state) if p.get("cpu") and not p["acted"]]
        for p in pending:
            if cpu_decision(state, p) == "stay":
                stay(state, p["userId"])
            else:
                fold(state, p["userId"])
        if hand_over(state) or round_complete(state):
            advance(state)
        else:
            break
    return state


def seat(state, user_id):
    return next((p for p in state["players"] if p["userId"] == user_id), None)


def active(state):
    """Players still in the hand."""
    return [p for p in state["players"] if not p["folded"]]


def deal(state):
    """Start the hand: hole cards for everyone, plus the flop."""
    deck = new_deck()
    for p in state["players"]:
        p["hole"] = [deck.pop(), deck.pop()]
        p["folded"] = False
        p["acted"] = False
    deck.pop()                                    # burn
    state["board"] = [deck.pop(), deck.pop(), deck.pop()]
    state["deck"] = deck
    state["stage"] = "flop"
    return state


def stay(state, user_id):
    p = seat(state, user_id)
    p["acted"] = True
    p["contributed"] += state["ante"]
    state["pot"] += state["ante"]
    return state


def fold(state, user_id):
    p = seat(state, user_id)
    p["folded"] = True
    p["acted"] = True
    return state


def round_complete(state):
    return all(p["acted"] for p in active(state))


def hand_over(state):
    """Only one player left, so there is nothing further to play.

    This has to be checked separately from `round_complete`: the survivor of a
    round of folds has not acted yet, so the round would otherwise sit there
    waiting for them forever.
    """
    return len(active(state)) <= 1


def advance(state):
    """Move to the next street once everyone has acted."""
    if len(active(state)) <= 1:
        return settle(state)

    order = list(STAGES)
    nxt = order[order.index(state["stage"]) + 1]
    if nxt == "showdown":
        return settle(state)

    deck = state["deck"]
    deck.pop()                                    # burn
    state["board"].append(deck.pop())
    state["stage"] = nxt
    for p in state["players"]:
        p["acted"] = p["folded"]
    return state


def settle(state):
    """Decide the winner(s) and split the pot."""
    state["stage"] = "showdown"
    contenders = active(state)

    if len(contenders) == 1:
        winner = contenders[0]
        state["result"] = {
            "winners": [winner["userId"]],
            "share": state["pot"],
            "text": "Everyone else folded.",
            "hands": {},
        }
        return state

    scored = {}
    for p in contenders:
        scored[p["userId"]] = best_hand(p["hole"] + state["board"])

    top = max(scored.values(), key=lambda h: h["score"])["score"]
    winners = [uid for uid, h in scored.items() if h["score"] == top]
    share = state["pot"] // len(winners)
    name = next(h["name"] for h in scored.values() if h["score"] == top)

    state["result"] = {
        "winners": winners,
        "share": share,
        "text": f"{name} takes it" if len(winners) == 1 else f"Split pot — {name}",
        "hands": {
            str(uid): {"name": h["name"], "cards": h["cards"]}
            for uid, h in scored.items()
        },
    }
    return state
