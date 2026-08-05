# Tests

Plain Python, no test framework. Most files talk to a running server over HTTP
and print PASS/FAIL lines.

`test_rules.py` is the exception: it imports `blackjack`, `poker`, `roulette`
and `db` directly and checks the pure rules — hand values, splits and doubles,
poker betting maths, roulette odds, the `/work` pay ladder. It needs no server
and no arguments:

```bash
python3 tests/test_rules.py
```

For the rest, start a server against a throwaway database so your real accounts
are never touched. `NEXUS_OPEN_SIGNUP=1` skips the approval queue, so the
suites can register the accounts they need:

```bash
NEXUS_OPEN_SIGNUP=1 NEXUS_DB=/tmp/nexus_test.db NEXUS_UPLOADS=/tmp/nexus_test_uploads python3 app.py --port 8130 --host 127.0.0.1
```

`test_signup_approval.py` needs the queue switched on, so it wants a second
server of its own. Point the admin address at nothing so the test can pick its
own administrator:

```bash
NEXUS_ADMIN_EMAIL=nobody@nowhere.invalid NEXUS_DB=/tmp/nexus_gate.db NEXUS_UPLOADS=/tmp/nexus_gate_uploads python3 app.py --port 8131 --host 127.0.0.1
```

```bash
python3 tests/test_signup_approval.py 8131 /tmp/nexus_gate.db
```

Then, from the project root:

```bash
python3 tests/test_purge_and_status.py 8130
```

The first argument is the port. `test_shop_polls_mutes.py` takes the database
path as a second argument (default `/tmp/nexus_test.db`): buying a 1,000,000
perk means putting a balance in place directly rather than playing for it.

Delete `/tmp/nexus_test.db` between runs — the tests register fixed email
addresses and will collide with themselves otherwise.

| File | Covers |
|---|---|
| `test_rules.py` | Game rules and wage maths, no server needed |
| `test_signup_approval.py` | The approval queue — needs its own gated server |
| `test_betting.py` | Poker check/call/raise, blackjack split and double, roulette |
| `test_shop_polls_mutes.py` | The shop and perks, the Sana Lounge, `/work`, `/reset`, mutes, polls |
| `test_poker.py` | Tables, seating, hole-card secrecy, showdown payouts |
| `test_economy.py` | Wallets, stakes, the bank and the leaderboard |
| `test_channels_and_icons.py` | Channel ordering, server icons |
| `test_gifs.py` | The Giphy proxy and favourites |
| `test_purge_and_status.py` | `/purge` and custom statuses |
