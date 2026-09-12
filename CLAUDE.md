# Notes for Claude

`CONTRIBUTING.md` is the source of truth for formatting, `tox` usage, the
no-`pytest` rule, and the PR checklist; read it first. What follows is only
the non-obvious operational knowledge that is easy to spend an afternoon
rediscovering.

## Establish a baseline before believing a failure

Some tests fail for reasons unrelated to your change, depending on which
libcurl is installed. Before chasing one, check out the base commit (or
`git stash`) and see whether it already failed. With libcurl 8.21,
`CurlHTTPClientReuseProxyAuthTestCase.test_reuse_proxy_credentials` fails on
untouched `master` and `branch6.5` alike.

The same goes for `mypy`: the count of pre-existing errors differs per branch
(5 on `master`, 0 on `branch6.5` with its older pin). "mypy is clean" only
means something relative to a baseline you actually measured.

## Use each branch's own pinned tools

`requirements.txt` pins `black`, `flake8` and `mypy`, and the pins differ
between branches — `master` has `black` 26.x where `branch6.5` has 24.4.2.
Running the wrong `black` reformats about a dozen files of untouched code and
buries your diff in noise. Install from the branch's own `requirements.txt`.

Run `flake8` from the repository root so that it picks up `setup.cfg`;
elsewhere it emits hundreds of spurious E501/E402.

## Testing curl_httpclient across libcurl versions

`pycurl` is an optional extra (the `-full` tox environments), not in
`requirements.txt`. Without libcurl development headers it cannot build from
source — `pip install pycurl` fails with `Could not run curl-config`. Install
a manylinux wheel instead, which bundles its own libcurl.

That bundling is also the cheapest way to get a version matrix, which matters
because this module's behaviour genuinely varies with libcurl:

| pycurl | bundled libcurl      |
| ------ | -------------------- |
| 7.45.3 | 7.61.1 (with brotli) |
| 7.45.4 | 8.11.1               |
| 7.45.6 | 8.12.1               |
| 7.45.7 | 8.16.0               |
| 7.47.0 | 8.21.0               |

    pip download pycurl==7.45.7 --no-deps -d /tmp/w
    mkdir /tmp/pc && (cd /tmp/pc && unzip -q /tmp/w/pycurl-*.whl)
    PYTHONPATH=/tmp/pc:$PWD python -m tornado.test.runtests \
        tornado.test.curl_httpclient_test

Running this matrix is how the 6.5.9 streaming fix turned up a stall that only
appears on libcurl older than 7.69. Anything touching `curl_httpclient.py`
deserves more than whichever version happens to be installed. Behaviours found
to be version-dependent so far: whether unpausing a transfer notifies the
application's timer callback (it does from 7.69), the 64MB cap on libcurl's own
paused-transfer buffer (from 7.71), and whether `CURLOPT_MAXFILESIZE` is
measured after content decoding (it is not, in every release through 8.18).
The documented floor is libcurl 7.81, the version in the oldest supported
Ubuntu LTS.

## Test suite specifics

* `--fail-if-logs=true` fails on anything logged at **INFO** or above, and on
  any bytes written to stderr. A test that legitimately provokes a `gen_log`
  info message needs `ExpectLog` around it.
* The per-test async timeout is 5 seconds (`ASYNC_TEST_TIMEOUT` overrides).
  Deliberately large payloads run into it.
* Tests shared by both HTTP client implementations belong in
  `httpclient_test.HTTPClientCommonTestCase`, which `simple_httpclient_test`
  and `curl_httpclient_test` both subclass. Note that it also runs a third
  time under its own name, against the default client.
* For any *other* test case both implementations should share, use
  `tornado.test.util.abstract_base_test` instead of writing a near-copy per
  implementation. It is already used by the httpserver, iostream, netutil,
  websocket and httpclient tests.
* `circlerefs_test.py` asserts that an operation leaves behind no reference
  *cycles* — a CPython performance concern, and a different thing from a leak.
  A C-level reference the GC cannot see (pycurl holding a callback, say) needs
  a separate weakref test; `skipNotCPython` guards both kinds.
* `HTTPClientCommonTestCase`'s application is built with `gzip=True`. The
  `GZipContentEncoding` transform skips a response that already carries a
  `Content-Encoding` header or has a non-compressible `Content-Type`, so a
  handler that wants to control its own encoding must arrange one of those.
  Otherwise the server gzips it underneath you and the test measures something
  other than what you meant.

## curl_httpclient internals worth knowing

* Easy handles are pooled and reused (`_free_list`, reset in `_finish`), so
  per-request state leaking into the following request is a live bug class:
  `test_reuse_proxy_credentials` and `test_reuse_certs` both exist because of
  it. New per-request state deserves a reuse test with `max_clients=1`.
* That state is smuggled in a dict attached to the handle (`curl.info`,
  rebuilt in `_process_queue`). There is a standing TODO about the approach;
  until it is addressed, this is where such state goes.

## Branches, backports and docs

* Upstream is `tornadoweb/tornado`; `master` is the next minor and `branch6.5`
  is the maintenance branch. Sync with upstream before starting — it may
  already have fixed the failure you are looking at.
* `branch6.5` is Python 3.9 with the older typing style (`Optional[X]`,
  `Dict[...]`, `Deque` under `TYPE_CHECKING`, `# type:` comments); `master` is
  3.11+ with `X | None`. When backporting, remember that **attribute and
  class-level annotations are evaluated at runtime**, so
  `self.chunks: Deque[bytes] = ...` raises `NameError` where `Deque` is a
  `TYPE_CHECKING`-only import. Function-local variable annotations are not
  evaluated. Use `# type:` comments for the former.
* Cherry-picking a multi-commit series across that gap conflicts over and over.
  Applying the net diff once is far less error-prone:
  `git diff upstream/master <branch> | git apply -3`. Re-read each conflict
  hunk against its surroundings afterwards — it is easy to keep a line that
  assumes state the target branch does not have, and neither `mypy` nor the
  linters will catch it when the state lives in an untyped dict.
* Release notes live in `docs/releases/vX.Y.Z.rst` and are written at release
  time, not per change. A `versionadded`/`versionchanged` should name the
  release the change first ships in, which for a backported fix is the patch
  release, on both branches.
* `docs/httpclient.rst` documents `SimpleAsyncHTTPClient` with `autoclass`
  (arguments described in the class docstring) but `CurlAsyncHTTPClient` with a
  hand-written `.. class::` directive. A new constructor argument for the curl
  client has to be added to that signature by hand.
* `setup.py` and some other files contain cog-generated regions
  (`# [[[cog ... ]]]`), and CI has a linter for cog divergence. Regenerate
  rather than hand-editing.

## Measure instead of reasoning about performance

Confident predictions about this codebase have a poor track record. Queuing one
IOLoop callback per 16KB chunk rather than batching cost about 7%, not the
large factor expected; and a "fast path" that bypassed a buffer for the common
case turned out to be no faster than the general path at all. Run the server in
a separate process so it does not share the client's IOLoop, interleave the
variants rather than running them in blocks, and take medians over several
runs — single runs here vary by 50%.
