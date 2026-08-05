"""Texas Hold'em for the Gamesman. Pure functions, no I/O.

Betting works the way it does at a real table. Each street opens with nothing
to call, so checking is free; putting money in raises the price for everyone
behind you, and anyone who had already acted owes the difference before the
street can close.

    join   — each player pays the ante into the pot
    deal   — two hole cards each, and the flop, then a betting round
    turn   — fourth community card, betting round
    river  — fifth community card, betting round
    showdown — best five cards from the seven wins the pot

Hands are ranked with the standard categories; ties split the pot.

Money is tracked here only as "how much has this seat put in" — holding it out
of a player's wallet is the caller's job, which is why `stay`/`raise_to` return
the amount that needs collecting.
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

# A raise is a multiple of the ante, which keeps the numbers round and stops
# anyone raising by 1 forever to stall a table.
MIN_RAISE_MULTIPLE = 1
MAX_RAISE_MULTIPLE = 20

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
        # The price of staying in this street. Zero at the top of every street,
        # which is what makes checking free.
        "toMatch": 0,
        "lastRaise": 0,
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
        # Put in during the current street only; reset when the street turns.
        "street": 0,
        # Everything they had is in the middle. They stay in the hand and are
        # never asked for another chip.
        "allIn": False,
    }


def owed(state, player):
    """What this player must put in to stay in the hand right now.

    Nothing, once they are all in: they have no more to give, and a raise
    behind them must not turn into a demand they cannot meet.

    `.get` rather than `[]`: a table dealt before betting existed has neither
    key, and reads as "nothing owed", which is what it was.
    """
    if player.get("allIn"):
        return 0
    return max(0, state.get("toMatch", 0) - player.get("street", 0))


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


# ------------------------------------------------------------------ equity

ALL_CARDS = tuple(f"{rank}{suit}" for suit in SUITS for rank in RANKS)


def trials_for(board, opponents):
    """How many deals to run.

    Each trial ranks every live hand, so the cost climbs with the table. The
    river needs no runout and is cheap; the flop deals two more cards and is
    the expensive one. These numbers keep a decision under about a tenth of a
    second, which is the budget: a table of house players must not make the
    person who clicked wait.
    """
    budget = 900 if len(board) >= 5 else 500
    return max(80, budget // max(1, opponents))


def equity(hole, board, opponents, trials=None):
    """Roughly how often this hand wins, by dealing the rest out at random.

    A tie counts as half, since the pot is split. This is what turns a bot
    that only knows "do I have a pair" into one that knows a pair of fours on
    a four-flush board against three players is worth very little.
    """
    if opponents <= 0:
        return 1.0
    if trials is None:
        trials = trials_for(board, opponents)

    known = set(hole) | set(board)
    deck = [c for c in ALL_CARDS if c not in known]
    needed = 5 - len(board)
    draw_size = needed + 2 * opponents
    if draw_size > len(deck):
        return 0.5

    rng = secrets.SystemRandom()
    score = 0.0
    for _ in range(trials):
        drawn = rng.sample(deck, draw_size)
        full_board = board + drawn[:needed]
        mine = best_hand(hole + full_board)["score"]
        best_other = None
        at = needed
        for _ in range(opponents):
            theirs = best_hand(list(drawn[at:at + 2]) + full_board)["score"]
            at += 2
            if best_other is None or theirs > best_other:
                best_other = theirs
        if mine > best_other:
            score += 1.0
        elif mine == best_other:
            score += 0.5
    return score / trials


def cpu_decision(state, player):
    """Decide by working out how often the hand actually wins, and what the
    pot is offering to find out.

    Returns one of "check", "call", "raise" or "fold". Calling is worth it
    when the chance of winning beats the price: paying 200 into an 800 pot
    needs 200/1000, or 20%, to break even. Anything better than that is a
    call whatever the cards look like, and anything worse is a fold however
    pretty they are — which is the whole difference between this and guessing
    from the hand category.
    """
    rng = secrets.SystemRandom()
    rivals = [p for p in active(state) if p is not player]
    if not rivals:
        return "check"

    chance = equity(player["hole"], state["board"], len(rivals))
    price = owed(state, player)

    if price == 0:
        # A free card. Bet when ahead, and now and then when hopeless, so a
        # bet from the house isn't a guarantee of a hand.
        if chance > 0.72:
            return "raise" if rng.random() < 0.7 else "check"
        if chance > 0.55:
            return "raise" if rng.random() < 0.3 else "check"
        if chance < 0.25 and rng.random() < 0.10:
            return "raise"
        return "check"

    # The share of the final pot being bought, which is what the chance of
    # winning has to beat.
    breakeven = price / (state["pot"] + price)

    if chance > breakeven + 0.25:
        return "raise" if rng.random() < 0.55 else "call"
    if chance > breakeven + 0.02:
        return "call"
    # A shade short: pay it occasionally rather than folding like clockwork,
    # or the house becomes trivially readable.
    if chance > breakeven - 0.05 and rng.random() < 0.2:
        return "call"
    return "fold"


def play_cpus(state):
    """Let the house players take their turns, advancing streets as they go.

    Stops as soon as a human still owes an action, so it never plays past
    someone's decision.
    """
    for _ in range(len(STAGES) * MAX_PLAYERS * 2):
        if state["stage"] in ("waiting", "showdown"):
            break
        pending = [p for p in active(state)
                   if p.get("cpu") and not p["acted"] and not p.get("allIn")]
        if not pending:
            if hand_over(state) or round_complete(state):
                advance(state)
                continue
            break
        for p in pending:
            choice = cpu_decision(state, p)
            if choice == "fold":
                fold(state, p["userId"])
            elif choice == "raise":
                raise_to(state, p["userId"], state["ante"])
            else:
                stay(state, p["userId"])
        if hand_over(state) or round_complete(state):
            advance(state)
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
        p["street"] = 0
    deck.pop()                                    # burn
    state["board"] = [deck.pop(), deck.pop(), deck.pop()]
    state["deck"] = deck
    state["stage"] = "flop"
    state["toMatch"] = 0
    state["lastRaise"] = 0
    return state


def stay(state, user_id, budget=None):
    """Check (nothing owed) or call. Returns what it actually cost.

    `budget` is what the player can afford. Short of the full price they go
    all in for the rest — which is the whole point: being outbet by someone
    with a deeper stack should not force you out of a hand you have already
    paid into.
    """
    p = seat(state, user_id)
    price = owed(state, p)
    cost = price if budget is None else min(price, max(0, budget))
    p["acted"] = True
    p["street"] = p.get("street", 0) + cost
    p["contributed"] += cost
    state["pot"] += cost
    if budget is not None and cost < price:
        p["allIn"] = True
    return cost


def raise_to(state, user_id, amount, budget=None):
    """Call what's owed and put `amount` more on top. Returns the total cost.

    Everyone still in has to answer the new price, so their turn reopens —
    that is the whole point of a raise and the reason a round can go round
    more than once. Only players who can still act are reopened; anyone
    already all in has nothing left to answer with.
    """
    p = seat(state, user_id)
    wanted = owed(state, p) + amount
    cost = wanted if budget is None else min(wanted, max(0, budget))
    p["acted"] = True
    p["street"] = p.get("street", 0) + cost
    p["contributed"] += cost
    state["pot"] += cost
    if budget is not None and cost < wanted:
        p["allIn"] = True
    # Putting in less than the going rate is a call, not a raise: it sets no
    # new price for anybody.
    if p["street"] > state.get("toMatch", 0):
        state["toMatch"] = p["street"]
        state["lastRaise"] = amount
        for other in active(state):
            if other is not p and not other.get("allIn"):
                other["acted"] = False
    return cost


def fold(state, user_id):
    p = seat(state, user_id)
    p["folded"] = True
    p["acted"] = True
    return state


def round_complete(state):
    """Everyone left has acted and has matched the price.

    An all-in player is counted as done however the betting goes: `owed`
    already reads zero for them, and nobody is waiting on a decision they
    cannot make.
    """
    return all(p["acted"] or p.get("allIn") for p in active(state)) and all(
        owed(state, p) == 0 for p in active(state)
    )


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
    # New street, new prices: nothing is owed until somebody bets.
    state["toMatch"] = 0
    state["lastRaise"] = 0
    for p in state["players"]:
        # All-in players sit the rest of the hand out without folding.
        p["acted"] = p["folded"] or p.get("allIn", False)
        p["street"] = 0
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

    payouts = award(state, contenders, scored)
    top = max(scored.values(), key=lambda h: h["score"])["score"]
    winners = [uid for uid, h in scored.items() if h["score"] == top]
    name = next(h["name"] for h in scored.values() if h["score"] == top)

    state["result"] = {
        "winners": [uid for uid in payouts if payouts[uid] > 0] or winners,
        # Kept for cards written before side pots existed, which read a single
        # figure. `payouts` is the real answer when it is there.
        "share": max(payouts.values()) if payouts else 0,
        "payouts": {str(uid): amount for uid, amount in payouts.items()},
        "text": f"{name} takes it" if len(winners) == 1 else f"Split pot — {name}",
        "hands": {
            str(uid): {"name": h["name"], "cards": h["cards"]}
            for uid, h in scored.items()
        },
    }
    return state


def award(state, contenders, scored):
    """Split the pot into side pots and give each one to the best hand in it.

    You can only win from someone what you also put in. Somebody all in for
    100 against two players who bet 5,000 wins 300 and no more; the rest is
    fought over by the players who could still cover it. Folded chips stay in
    the pot and are won by whoever the layer belongs to.

    Returns {user_id: amount won}.
    """
    payouts = {p["userId"]: 0 for p in contenders}
    # Each distinct contribution among the players still in marks the top of
    # one layer of the pot.
    levels = sorted({p["contributed"] for p in contenders if p["contributed"] > 0})
    floor_ = 0
    for level in levels:
        # Everyone who reached this layer paid into it, folded or not.
        amount = sum(
            max(0, min(p["contributed"], level) - floor_) for p in state["players"]
        )
        eligible = [p for p in contenders if p["contributed"] >= level]
        floor_ = level
        if not amount or not eligible:
            continue
        best = max(scored[p["userId"]]["score"] for p in eligible)
        takers = [p["userId"] for p in eligible if scored[p["userId"]]["score"] == best]
        share, odd = divmod(amount, len(takers))
        for uid in takers:
            payouts[uid] += share
        # An indivisible chip goes to the first of them rather than vanishing.
        if odd:
            payouts[takers[0]] += odd
    return payouts
