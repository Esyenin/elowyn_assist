# Hindsight 0.9.1 integration gate

Run the mandatory real-backend Memory integration tests with one command:

```bash
python scripts/run_hindsight_integration.py
```

Prerequisites are Docker and the project test environment installed with the
`memory` extra (`python -m pip install -e ".[memory,test]"`). The command:

1. starts the exact image `ghcr.io/vectorize-io/hindsight:0.9.1` on a random
   loopback port;
2. uses Hindsight's own deterministic `mock` LLM provider and embedded `pg0`
   database, so no API keys or other secrets are required;
3. waits for `/health/ready`;
4. passes `ELOWYN_TEST_HINDSIGHT_URL` only to the integration-test process and
   fails the gate on any skip;
5. removes the ephemeral container and all of its test banks on exit.

The same command runs in the `memory-integration` GitHub Actions job. The
container receives no Core database URL or credentials.
