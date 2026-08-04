"""Blackjack rules for the Gamesman bot. Pure functions, no I/O.

Two shapes of game:

* ``cpu`` — one player against the dealer, standard rules: the dealer reveals
  its hole card once the player stands and then draws to 17. The player may
  double down and split, so a cpu game holds a *list* of player hands.
* ``pvp`` — two players race each other. There is no dealer; whoever ends
  closest to 21 without busting wins. Doubling and splitting are out: both
  seats put up a matched stake, and there is no way to match half a split.

State is a plain dict so it can be stored as JSON in the games table. Hands
dealt before splitting existed have no ``player`` list; `player_hands` builds
one from the old single hand, so old cards in channel history still render.
"""

import secrets

SUITS = ("♠", "♥", "♦", "♣")
RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")

DEALER_STANDS_ON = 17
BLACKJACK = 21

# Split, then split again: four hands is where every casino stops too.
MAX_HANDS = 4


def new_deck():
    """A shuffled 52-card deck. SystemRandom so hands can't be predicted."""
    deck = [f"{rank}{suit}" for suit in SUITS for rank in RANKS]
    secrets.SystemRandom().shuffle(deck)
    return deck


def rank_of(card):
    return card[:-1]


def card_value(card):
    rank = rank_of(card)
    if rank == "A":
        return 11
    if rank in ("J", "Q", "K"):
        return 10
    return int(rank)


def hand_value(cards):
    """Best total that doesn't bust; aces drop from 11 to 1 as needed."""
    total = sum(card_value(c) for c in cards)
    aces = sum(1 for c in cards if rank_of(c) == "A")
    while total > BLACKJACK and aces:
        total -= 10
        aces -= 1
    return total


def is_bust(cards):
    return hand_value(cards) > BLACKJACK


def is_natural(cards):
    """21 on the first two cards."""
    return len(cards) == 2 and hand_value(cards) == BLACKJACK


# ------------------------------------------------------------------ lifecycle

def new_hand(cards, bet=1):
    """One player hand. `bet` is a multiple of the table stake, so a doubled
    hand is 2 and the payout maths never needs to know how it got there."""
    return {"cards": cards, "stood": False, "doubled": False, "bet": bet}


def new_game(mode):
    """Deal the opening hands. In cpu mode the dealer's second card is hidden."""
    deck = new_deck()
    host = [deck.pop(), deck.pop()]
    opp = [deck.pop(), deck.pop()]
    state = {
        "deck": deck,
        "hands": {"host": host, "opp": opp},
        "stood": {"host": False, "opp": False},
        "turn": "host",
        "result": None,
    }
    if mode == "cpu":
        state["player"] = [new_hand(host)]
        state["active"] = 0
        state["split"] = False
        state["results"] = []
        # A natural ends the hand immediately, exactly like a real table.
        if is_natural(host) or is_natural(opp):
            play_dealer(state)
    return state


def player_hands(state):
    """The player's hands, upgrading a state written before splitting existed."""
    if "player" not in state:
        hand = new_hand(state["hands"]["host"])
        hand["stood"] = state["stood"]["host"]
        state["player"] = [hand]
        state["active"] = 0
    return state["player"]


def current_hand(state):
    """The hand the player is acting on."""
    hands = player_hands(state)
    return hands[min(state.get("active", 0), len(hands) - 1)]


def hand_done(hand):
    return hand["stood"] or is_bust(hand["cards"])


def can_act(state, seat):
    if state["result"] is not None or state["turn"] != seat:
        return False
    if seat == "host" and "player" in state:
        return not hand_done(current_hand(state))
    return not state["stood"][seat] and not is_bust(state["hands"][seat])


def can_double(state):
    """Doubling is a first-decision move: two cards, nothing drawn yet."""
    if state["result"] is not None or "player" not in state:
        return False
    hand = current_hand(state)
    return len(hand["cards"]) == 2 and not hand_done(hand) and not hand["doubled"]


def can_split(state):
    """Two cards of the same value, and room for another hand."""
    if state["result"] is not None or "player" not in state:
        return False
    hand = current_hand(state)
    if len(hand["cards"]) != 2 or hand_done(hand):
        return False
    if len(state["player"]) >= MAX_HANDS:
        return False
    a, b = hand["cards"]
    return card_value(a) == card_value(b)


def hit(state, seat, mode):
    """Draw one card for `seat`, advancing the turn if that ends their go."""
    if mode == "cpu":
        hand = current_hand(state)
        hand["cards"].append(state["deck"].pop())
        _mirror(state)
        if is_bust(hand["cards"]) or hand_value(hand["cards"]) == BLACKJACK:
            # Busting ends your turn; so does hitting exactly 21 — there's no
            # reason to draw again and it saves a pointless click.
            stand(state, seat, mode)
        return state

    state["hands"][seat].append(state["deck"].pop())
    if is_bust(state["hands"][seat]) or hand_value(state["hands"][seat]) == BLACKJACK:
        stand(state, seat, mode)
    return state


def stand(state, seat, mode):
    if mode == "cpu":
        current_hand(state)["stood"] = True
        _mirror(state)
        _next_hand(state)
        return state

    state["stood"][seat] = True
    _advance(state, mode)
    return state


def double(state):
    """Twice the stake, exactly one more card, then the hand is over."""
    hand = current_hand(state)
    hand["doubled"] = True
    hand["bet"] = hand["bet"] * 2
    hand["cards"].append(state["deck"].pop())
    hand["stood"] = True
    _mirror(state)
    _next_hand(state)
    return state


def split(state):
    """Turn one pair into two hands, each drawing a second card.

    Split aces get one card each and stand, which is the near-universal house
    rule — otherwise splitting aces would be too strong to ever not do.
    """
    hands = player_hands(state)
    index = state["active"]
    hand = hands[index]
    moved = hand["cards"].pop()
    aces = rank_of(moved) == "A"

    hand["cards"].append(state["deck"].pop())
    fresh = new_hand([moved, state["deck"].pop()], bet=hand["bet"])
    hands.insert(index + 1, fresh)
    state["split"] = True

    for h in (hand, fresh):
        # Split aces stand on their one card; so does any hand that lands on
        # 21, for the same reason hitting 21 ends your turn.
        if aces or hand_value(h["cards"]) == BLACKJACK:
            h["stood"] = True
    _mirror(state)
    if hand_done(hand):
        _next_hand(state)
    return state


def _mirror(state):
    """Keep the legacy single-hand fields pointing at hand one.

    Nothing in the app reads them for a cpu game any more, but a card posted
    today may be re-rendered by a build that does.
    """
    hands = player_hands(state)
    state["hands"]["host"] = hands[0]["cards"]
    state["stood"]["host"] = hands[0]["stood"]


def _next_hand(state):
    """Move to the next unfinished hand, or hand over to the dealer."""
    hands = player_hands(state)
    index = state.get("active", 0)
    while index + 1 < len(hands):
        index += 1
        if not hand_done(hands[index]):
            state["active"] = index
            return state
    state["active"] = len(hands) - 1
    play_dealer(state)
    return state


def _advance(state, mode):
    if mode == "cpu":
        if _seat_done(state, "host"):
            play_dealer(state)
        return

    if _seat_done(state, "host") and _seat_done(state, "opp"):
        state["turn"] = None
        settle(state, mode)
    elif _seat_done(state, "host"):
        state["turn"] = "opp"
    else:
        state["turn"] = "host"


def _seat_done(state, seat):
    return state["stood"][seat] or is_bust(state["hands"][seat])


def play_dealer(state):
    """Dealer reveals and draws to 17, then the hand is settled."""
    state["turn"] = "opp"
    dealer = state["hands"]["opp"]
    # No reason to draw against a table of busted hands.
    if any(not is_bust(h["cards"]) for h in player_hands(state)):
        while hand_value(dealer) < DEALER_STANDS_ON:
            dealer.append(state["deck"].pop())
    state["stood"]["opp"] = True
    state["turn"] = None
    settle(state, "cpu")


def settle(state, mode):
    """Decide the winner and write a human-readable line."""
    if mode == "cpu":
        return _settle_cpu(state)

    host, opp = state["hands"]["host"], state["hands"]["opp"]
    hv, ov = hand_value(host), hand_value(opp)

    if is_bust(host) and is_bust(opp):
        winner, text = "push", "Both busted — nobody wins."
    elif is_bust(host):
        winner, text = "opp", f"You busted with {hv}."
    elif is_bust(opp):
        winner, text = "host", f"Opponent busted with {ov}."
    elif hv > ov:
        winner, text = "host", f"{hv} beats {ov}."
    elif ov > hv:
        winner, text = "opp", f"{ov} beats {hv}."
    else:
        winner, text = "push", f"Both on {hv} — push."

    if winner == "host" and is_natural(host):
        text = "Blackjack!"
    state["result"] = {"winner": winner, "text": text}
    return state


def _settle_cpu(state):
    """Score every player hand against the dealer, then sum them up."""
    dealer = state["hands"]["opp"]
    dv = hand_value(dealer)
    hands = player_hands(state)

    results = []
    for hand in hands:
        cards = hand["cards"]
        hv = hand_value(cards)
        if is_bust(cards):
            winner, text = "opp", f"Busted with {hv}."
        elif is_bust(dealer):
            winner, text = "host", f"Dealer busted with {dv}."
        elif hv > dv:
            winner, text = "host", f"{hv} beats {dv}."
        elif dv > hv:
            winner, text = "opp", f"{dv} beats {hv}."
        else:
            winner, text = "push", f"Both on {hv} — push."
        results.append({"winner": winner, "text": text})
    state["results"] = results

    # One headline for the card, in the same shape a single hand has always
    # had. Stakes differ between hands once one is doubled, so the summary is
    # weighted by them rather than counting hands.
    won = sum(h["bet"] for h, r in zip(hands, results) if r["winner"] == "host")
    lost = sum(h["bet"] for h, r in zip(hands, results) if r["winner"] == "opp")
    if len(hands) == 1:
        text = results[0]["text"]
        if results[0]["winner"] == "host" and not state.get("split") \
                and is_natural(hands[0]["cards"]):
            text = "Blackjack!"
        winner = results[0]["winner"]
    else:
        winner = "host" if won > lost else ("opp" if lost > won else "push")
        parts = [f"Hand {i + 1}: {hand_value(h['cards'])}" for i, h in enumerate(hands)]
        text = f"Dealer {dv}. " + ", ".join(parts) + "."
    state["result"] = {"winner": winner, "text": text}
    return state


def visible_hand(state, seat, mode, viewer_seat):
    """Cards this viewer is allowed to see.

    Nothing is hidden once the hand has settled. Before then:

    * cpu — the dealer keeps one card face down, as at a real table.
    * pvp — you only ever see your own cards. Showing both would hand the
      second player a decisive advantage, since they could read the total
      they need before choosing to draw. Spectators see neither hand.
    """
    cards = list(state["hands"][seat])
    if state["result"] is not None:
        return cards

    if mode == "cpu":
        if seat == "opp" and len(cards) > 1:
            return cards[:1] + ["??"] * (len(cards) - 1)
        return cards

    if seat != viewer_seat:
        return ["??"] * len(cards)
    return cards
