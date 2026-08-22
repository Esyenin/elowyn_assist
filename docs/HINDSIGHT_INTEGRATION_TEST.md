# Hindsight 0.9.1 integration gate

Run the mandatory real-backend Memory integration tests with one command:

```bash
python scripts/run_hindsight_integration.py
```

Prerequisites are Docker, a migrated PostgreSQL acceptance database exposed as
`TEST_DATABASE_URL`, and the project test environment installed with the
`memory` extra (`python -m pip install -e ".[memory,test]"`). The command:

1. starts the exact image `ghcr.io/vectorize-io/hindsight:0.9.1` on a random
   loopback port;
2. uses Hindsight's own deterministic `mock` LLM provider and embedded `pg0`
   database, so no API keys or other secrets are required;
3. disables backend auto-consolidation so Elowyn-owned observations/pages are
   exercised without asynchronously replacing atomic provenance-bearing facts;
4. waits for `/health/ready`;
5. passes the Hindsight URL/container identity only to the integration-test process and
   fails the gate on any skip;
6. removes the ephemeral container and all of its test banks on exit.

The same command runs in the `memory-integration` GitHub Actions job. The
container receives no Core database URL or credentials.
