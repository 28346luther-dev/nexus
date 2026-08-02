# Tests

Plain Python, no test framework. Each file talks to a running server over HTTP
and prints PASS/FAIL lines.

Start a server against a throwaway database so your real accounts are never
touched:

```bash
NEXUS_DB=/tmp/nexus_test.db NEXUS_UPLOADS=/tmp/nexus_test_uploads python3 app.py --port 8130 --host 127.0.0.1
```

Then, from the project root:

```bash
python3 tests/test_purge_and_status.py 8130
```

The argument is the port. Delete `/tmp/nexus_test.db` between runs — the tests
register fixed email addresses and will collide with themselves otherwise.
