# HTTPX: matched native and purified failure

This opt-in diagnostic records a failure, not a passing compatibility point.
It uses HTTPX `b5addb64f0161ff6bfe94c124ef76f6a1fba5254`, pip-purifier
`77c5742ae034a12d2a45b4212290be862e0fa772`, Python 3.13.16, native pip
26.1.2, Uvicorn 0.35.0, pytest 8.4.1 and trio 0.31.0 on x86_64-linux.
The committed lock fixes the captured dependency artifacts and manifests.

From the repository root, run the two diagnostics separately:

```sh
nix build --impure --no-link --file tests/real-world/httpx-python313/reproduce.nix pure.tests
nix build --impure --no-link --file tests/real-world/httpx-python313/reproduce.nix raw
```

Both commands are expected to fail. Both run `pytest -q -m 'not network'`
with a 180-second bound in a network-disabled Nix sandbox. The local test
server is allowed. The selection excludes the five upstream tests marked
as requiring external networking; assertions and warning policy are unchanged.
The raw baseline installs the original requirements with ordinary pip from
the same captured wheelhouse, then compares every installed distribution's
normalized name and version with the purified environment before testing.
It does not invoke the purifier installer.

Both observed runs completed with **1,411 passed, one failed, one skipped
and five deselected**. Both failed `test_write_timeout[trio]` with
`PytestUnraisableExceptionWarning` from `ByteStream.__aiter__`. The
distribution comparison passed. This reproduces the failure independently
of purifier installation at this exact tuple; it establishes neither a
passing suite nor support for a version interval. See [receipt.json](receipt.json)
for the original derivation identities and source/lock hashes. The tested
topic tree and merged source tree were compared and are identical.

The Python 3.14 survey failure is separate: pinned Uvicorn 0.35.0 calls
`asyncio.iscoroutinefunction`, whose deprecation warning becomes an error
under HTTPX's policy; its fixture then waits indefinitely for server startup.
Python 3.13 avoids that startup failure but exposes the trio failure above.
No source patch, warning suppression or exclusion of the failing test was used.

Earlier controls remain distinct in the receipt: a host-side test was
blocked by the execution layer's `www.example.com` allowlist; its escalated
retry was canceled without a result. An initial raw baseline using
`python -I` also failed the SSLKEYLOGFILE test, because that launcher ignores
environment variables. The final baseline uses the same pytest console
invocation as the purified path.

Successful lock capture, offline development replay, pure imports and release
wheel/sdist construction establish the editable-extras ownership repair.
They do not turn either failing upstream test context into a pass.
