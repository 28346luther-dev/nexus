"""Blackjack rules for the Gamesman bot. Pure functions, no I/O.

Two shapes of game:

* ``cpu`` — one player against the dealer, standard rules: the dealer reveals
  its hole card once the player stands and then draws to 17.
* ``pvp`` — two players race each other. There is no dealer; whoever ends
  closest to 21 without busting wins.

State is a plain dict so it can be stored as JSON in the games table.
"""

import secrets

SUITS = ("♠", "♥", "♦", "♣")
RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")

DEALER_STANDS_ON = 17
BLACKJACK = 21


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
        # A natural ends the hand immediately, exactly like a real table.
        if is_natural(host) or is_natural(opp):
            play_dealer(state)
    return state


def can_act(state, seat):
    return (
        state["result"] is None
        and state["turn"] == seat
        and not state["stood"][seat]
        and not is_bust(state["hands"][seat])
    )


def hit(state, seat, mode):
    """Draw one card for `seat`, advancing the turn if that ends their go."""
    state["hands"][seat].append(state["deck"].pop())
    if is_bust(state["hands"][seat]) or hand_value(state["hands"][seat]) == BLACKJACK:
        # Busting ends your turn; so does hitting exactly 21 — there's no
        # reason to draw again and it saves a pointless click.
        stand(state, seat, mode)
    return state


def stand(state, seat, mode):
    state["stood"][seat] = True
    _advance(state, mode)
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
    if not is_bust(state["hands"]["host"]):
        while hand_value(dealer) < DEALER_STANDS_ON:
            dealer.append(state["deck"].pop())
    state["stood"]["opp"] = True
    state["turn"] = None
    settle(state, "cpu")


def settle(state, mode):
    """Decide the winner and write a human-readable line."""
    host, opp = state["hands"]["host"], state["hands"]["opp"]
    hv, ov = hand_value(host), hand_value(opp)
    dealer_word = "Dealer" if mode == "cpu" else "Opponent"

    if is_bust(host) and is_bust(opp):
        winner, text = "push", f"Both busted — nobody wins."
    elif is_bust(host):
        winner, text = "opp", f"You busted with {hv}."
    elif is_bust(opp):
        winner, text = "host", f"{dealer_word} busted with {ov}."
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
