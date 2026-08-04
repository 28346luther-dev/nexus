"""Rules that are pure functions: no server, no database, no port argument.

    python3 tests/test_rules.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point the storage layer at a path it will never open — importing db must not
# touch the real database just to read its constants.
os.environ.setdefault("NEXUS_DB", "/tmp/nexus_rules_unused.db")

import blackjack            # noqa: E402
import db                   # noqa: E402
import poker                # noqa: E402
import roulette            # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f'{"PASS" if cond else "FAIL"}  {name}' + (f"   {detail}" if detail and not cond else ""))


# ------------------------------------------------------------------- wages
check("a new starter is on 2,000", db.work_rate(0) == 2000, db.work_rate(0))
check("no rise before five hours", db.work_rate(4) == 2000, db.work_rate(4))
check("five hours earns 20%", db.work_rate(5) == 2400, db.work_rate(5))
check("rises compound", db.work_rate(10) == 2880, db.work_rate(10))
check("and keep compounding", db.work_rate(15) == 3456, db.work_rate(15))
check("the pay tops out at 10,000", db.work_rate(45) == 10_000, db.work_rate(45))
check("and stays there", db.work_rate(500) == 10_000, db.work_rate(500))
check("pay never goes backwards",
      all(db.work_rate(n) <= db.work_rate(n + 1) for n in range(0, 200)))
check("the countdown to a rise runs 5 to 1",
      [db.shifts_to_raise(n) for n in range(5)] == [5, 4, 3, 2, 1],
      [db.shifts_to_raise(n) for n in range(5)])
check("no countdown once the pay has topped out", db.shifts_to_raise(45) == 0)
check("the gold pass doubles the rate",
      db.work_pay({"goldpass"}, 10) == db.work_rate(10) * 2)
check("the vault halves the wait",
      db.work_interval({"vault"}) == db.work_interval(set()) // 2)
check("one shift in five is a write-off", db.WORK_SUCCESS == 0.8, db.WORK_SUCCESS)

# ------------------------------------------------------------------- shop
check("every shop item has a price and a name",
      all(i["price"] > 0 and i["name"] and i["summary"] for i in db.SHOP_ITEMS))
check("shop ids are unique",
      len({i["id"] for i in db.SHOP_ITEMS}) == len(db.SHOP_ITEMS))
check("the nameplate is 100,000", db.SHOP_BY_ID["glow"]["price"] == 100_000)
check("the lounge is 1,000,000", db.SHOP_BY_ID["lounge"]["price"] == 1_000_000)

# --------------------------------------------------------------- roulette
check("the wheel has 37 pockets", roulette.POCKETS == 37)
check("zero is green", roulette.colour_of(0) == "green")
check("reds and blacks split the other 36 evenly",
      sum(1 for n in range(1, 37) if roulette.colour_of(n) == "red") == 18
      and sum(1 for n in range(1, 37) if roulette.colour_of(n) == "black") == 18)
check("a straight number pays 35 to 1",
      roulette.play("number", 10, number=7, landed=7)["payout"] == 360)
check("a straight number pays nothing otherwise",
      roulette.play("number", 10, number=7, landed=8)["payout"] == 0)
check("red pays even money", roulette.play("red", 10, landed=3)["payout"] == 20)
check("a dozen pays 2 to 1", roulette.play("dozen1", 10, landed=5)["payout"] == 30)
check("zero beats every outside bet",
      all(roulette.play(kind, 10, landed=0)["payout"] == 0
          for kind in ("red", "black", "odd", "even", "low", "high",
                       "dozen1", "dozen2", "dozen3", "col1", "col2", "col3")))
check("every pocket is covered by exactly one colour bet",
      all(sum(1 for k in ("red", "black") if roulette.BETS[k]["hits"](n)) == 1
          for n in range(1, 37)))
check("the columns cover every number once",
      all(sum(1 for k in ("col1", "col2", "col3") if roulette.BETS[k]["hits"](n)) == 1
          for n in range(1, 37)))

# --------------------------------------------------------------- blackjack
check("aces drop from 11 to 1 when they have to",
      blackjack.hand_value(["A♠", "9♥", "5♦"]) == 15,
      blackjack.hand_value(["A♠", "9♥", "5♦"]))
check("two aces are 12", blackjack.hand_value(["A♠", "A♥"]) == 12)
check("a natural is 21 on two cards", blackjack.is_natural(["A♠", "K♥"]))
check("21 on three cards is not a natural",
      not blackjack.is_natural(["7♠", "7♥", "7♦"]))


def cpu_state(player_cards, dealer_cards):
    """A cpu game hand-built to a known position."""
    return {
        "deck": ["2♠"] * 20,
        "hands": {"host": list(player_cards), "opp": list(dealer_cards)},
        "stood": {"host": False, "opp": False},
        "turn": "host",
        "result": None,
        "player": [blackjack.new_hand(list(player_cards))],
        "active": 0,
        "split": False,
        "results": [],
    }


state = cpu_state(["8♠", "8♥"], ["9♦", "6♣"])
check("a pair can be split", blackjack.can_split(state))
check("a fresh hand can be doubled", blackjack.can_double(state))

state = cpu_state(["8♠", "K♥"], ["9♦", "6♣"])
check("two different cards can't be split", not blackjack.can_split(state))
check("a ten and a king can be split",
      blackjack.can_split(cpu_state(["10♠", "K♥"], ["9♦", "6♣"])))

state = cpu_state(["8♠", "8♥"], ["9♦", "6♣"])
blackjack.split(state)
check("splitting makes two hands", len(state["player"]) == 2, state["player"])
check("each split hand has two cards",
      all(len(h["cards"]) == 2 for h in state["player"]), state["player"])
check("the hand is marked as split", state["split"] is True)

state = cpu_state(["A♠", "A♥"], ["9♦", "6♣"])
blackjack.split(state)
check("split aces stand on one card each",
      all(h["stood"] for h in state["player"]), state["player"])

state = cpu_state(["5♠", "6♥"], ["9♦", "6♣"])
blackjack.double(state)
check("doubling draws exactly one card",
      len(state["player"][0]["cards"]) == 3, state["player"])
check("doubling doubles the stake", state["player"][0]["bet"] == 2)
check("doubling ends the hand", state["result"] is not None)

state = cpu_state(["10♠", "10♥"], ["9♦", "6♣"])
blackjack.split(state)
state["player"][0]["cards"] = ["10♠", "A♦"]      # 21, but out of a split
state["player"][0]["stood"] = True
state["player"][1]["cards"] = ["10♥", "5♦"]
state["player"][1]["stood"] = True
blackjack.play_dealer(state)
check("a split hand gets its own result", len(state["results"]) == 2, state["results"])
check("a 21 out of a split is not a blackjack",
      state["result"]["text"] != "Blackjack!", state["result"])

# ------------------------------------------------------------------ poker
table = poker.new_table(1, 100)
table["players"].append(poker.new_player(2))
for p in table["players"]:
    p["contributed"] = 100
table["pot"] = 200
poker.deal(table)

check("a street opens with nothing owed",
      poker.owed(table, poker.seat(table, 1)) == 0)
check("checking costs nothing", poker.stay(table, 1) == 0)
check("a check doesn't touch the pot", table["pot"] == 200, table["pot"])
check("checking is not enough to close a street on its own",
      not poker.round_complete(table))

cost = poker.raise_to(table, 2, 100)
check("a raise costs the raise", cost == 100, cost)
check("a raise goes into the pot", table["pot"] == 300, table["pot"])
check("a raise sets a price", table["toMatch"] == 100, table["toMatch"])
check("a raise reopens the checker's turn",
      not poker.seat(table, 1)["acted"])
check("the checker now owes the raise",
      poker.owed(table, poker.seat(table, 1)) == 100)
check("a street with money owed is not complete", not poker.round_complete(table))

check("calling costs exactly what's owed", poker.stay(table, 1) == 100)
check("the street closes once the raise is called", poker.round_complete(table))

poker.advance(table)
check("the next street is free again",
      table["toMatch"] == 0 and all(p["street"] == 0 for p in table["players"]),
      table["toMatch"])
check("the turn is the fourth card", len(table["board"]) == 4, table["board"])
check("the pot survives the street", table["pot"] == 400, table["pot"])

check("folding leaves one player standing",
      (poker.fold(table, 2), poker.hand_over(table))[1])

# Hand ranking still holds up.
check("a flush beats a straight",
      poker.best_hand(["2♠", "5♠", "9♠", "J♠", "K♠", "3♥", "4♦"])["score"]
      > poker.best_hand(["5♠", "6♥", "7♦", "8♣", "9♠", "2♥", "3♦"])["score"])
check("the wheel is a straight",
      poker.best_hand(["A♠", "2♥", "3♦", "4♣", "5♠", "K♦", "9♣"])["name"] == "Straight")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
