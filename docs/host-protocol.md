# Host protocol, version 1

How a front end talks to the download core when the two are not in the same
process. Implemented in `src/video_downloader/host/`, exercised end to end by
`tests/host/test_host_conformance.py`.

The core has no user interface of its own. It is started as a child process,
serves exactly one front end, and exits when that front end tells it to or when
its own process is ended.

## Transport

A TCP socket on `127.0.0.1`, port 0 - the operating system picks a free one.

A socket rather than a pipe, and the reason is not preference. The download
engine drives libcurl through `loop.add_reader`, which on Windows requires a
selector event loop, and a selector loop on Windows can watch sockets and
nothing else. A pipe would need a dedicated reader thread; `asyncio.start_server`
needs nothing, and the same code runs unchanged on macOS and Linux.

Measured on Windows 11 on 2026-09-13: binding a listener to `127.0.0.1`
triggers no Windows Defender firewall prompt. Not yet measured on macOS or
Linux.

## Handshake

The core prints exactly one line on stdout and then never writes there again:

```json
{"protocol":1,"port":54321,"token":"…32 hex characters…"}
```

Logging goes to the log file and to stderr. A log line in stdout would be
indistinguishable from a message, so `configure_logging` is given stderr by the
host.

The token is not a secret against the user - it is proof of identity. The port
is guessable and any local process may connect to it; the token says which
connection belongs to the front end that started this core. It is printed on
stdout, which only the parent process can read.

## Framing

One JSON object per line, UTF-8, terminated by `\n`. No length prefix and no
header: a line is a message. Non-ASCII travels as itself rather than escaped.

A line longer than 8 MiB closes the connection. A line that is not a JSON object
with a string `type` is answered with a `bad_request` result and the connection
stays open.

## Message kinds

| Kind | Direction | Carries | Who waits |
|---|---|---|---|
| Command | front end → core | `id` | the front end, for one `result` |
| Result | core → front end | the command's `id` | nobody |
| Event | core → front end | no id | nobody |
| Ask | core → front end | `askId` | **the core**, for one `ask.reply` |

### Results

```json
{"type":"result","id":7,"ok":true,"data":{…}}
{"type":"result","id":7,"ok":false,"error":{"code":"not_found","message":"…"}}
```

Error codes: `unauthenticated`, `unknown_command`, `bad_request`, `not_found`,
`failed`.

## Commands

The first message must be `hello`. Anything else before it is answered with
`unauthenticated`.

| Command | Fields | Result data |
|---|---|---|
| `hello` | `token` | `protocol`, `jobs[]` |
| `jobs.list` | — | `jobs[]` |
| `jobs.add` | `url`, `quality?`, `outputDir?` | `job` |
| `jobs.cancel` | `jobId` | `job` |
| `jobs.delete` | `jobId`, `deleteFile` | — |
| `jobs.rescan` | — | `jobs[]` |
| `settings.get` | — | `downloadDirectory` |
| `settings.setDownloadDirectory` | `path` | `downloadDirectory` |
| `sessions.list` | — | `sites[]`, `persists` |
| `sessions.put` | `site`, `cookies[]` | `persisted` |
| `sessions.clear` | `site` | `removed` |
| `app.shutdown` | — | — |
| `ask.reply` | `askId`, `value` | *(no result)* |

`deleteFile` has no default and a missing one is `bad_request`. For an entry
found by a directory scan, deleting removes a real video file that nobody
downloaded in this session, so the decision is never implied.

## Events

```json
{"type":"job.created","job":{…}}
{"type":"job.changed","job":{…}}
{"type":"job.removed","jobId":"…"}
```

`job.created` is sent for a job the front end asked for; jobs that already
existed when it connected arrive in the `hello` result instead. `job.changed`
is sent on every change to a job - the core already coalesces progress to at
most one update every 0.15 s before it gets here.

An event can overtake the result of the command that caused it. A front end
reading for a specific result must skip events rather than assume ordering.

### The job object

| Field | Notes |
|---|---|
| `id`, `url`, `title` | |
| `state` | `created`, `queued`, `connecting`, `fetching_metadata`, `downloading`, `muxing`, `completed`, `cancelled`, `failed` |
| `progress` | 0-100, meaningful only when `hasKnownTotal` |
| `done`, `total`, `unit` | `unit` is `segments` or `bytes` |
| `hasKnownTotal` | `false` means the end is unknown, **not** that nothing is done |
| `outputFile` | string or `null` |
| `expectedBytes` | `null` when the provider states no size |
| `error` | `null`, or `{kind, code, message, retryable, text}` |
| `fromDisk` | `true` for an entry found by a directory scan |
| `seq` | increases on every change; two observations are comparable by it |

Error kinds: `selection`, `unsupported`, `refusal`, `login_required`,
`too_large`, `track`, `mux`, `filesystem`, `incomplete`, `unknown`. The
exception behind a failure stays in the core's log and never travels.

## Asks

Two questions the core cannot answer itself. Both were callables on the job
before there was a protocol.

```json
{"type":"ask.confirmLargeDownload","askId":"…","jobId":"…","title":"…","estimatedBytes":3221225472}
{"type":"ask.login","askId":"…","site":"x.com","loginUrl":"https://x.com/login","requiredCookies":["auth_token","ct0"]}
```

The front end answers with `ask.reply` carrying the same `askId`:

* for the size question, `value` is a boolean.
* for the login, `value` is `null` when the user cancelled, or
  `{"cookies":[{"name":…,"value":…,"domain":…}]}`. The core builds the session
  value and stores it; a front end never touches the session store, and cannot
  store a session for a site it was not asked about.

### When no answer comes

A job is blocked on each of these, so all three failure modes are defined:

| Situation | What happens |
|---|---|
| No front end connected | the ask fails at once, without waiting |
| The connection drops while open | every pending ask fails immediately |
| Nobody answers in time | 300 s for the size question, 900 s for a login |

In every case the core takes the conservative answer: the download does not
start, and the refusal stands. Starting several gigabytes because the window
that was meant to ask is gone is the failure the question exists to prevent.

## One front end

A second connection is closed immediately rather than queued. This is a core
serving its own window, not a server: with two front ends an ask would have no
defined recipient, and two job lists would drift apart.

When the front end disconnects, the core fails every open ask and detaches from
every job it was watching. It keeps running - the downloads continue - and
accepts a new connection.

## Versioning

`protocol` is stated in the handshake, not negotiated. A front end that does not
recognise the number should refuse to start rather than guess what changed:
there is exactly one core and one front end, shipped together, and a version
skew between them is a packaging fault rather than a situation to recover from.

## Running it

```powershell
poe -C $repo host          # start the core; prints the handshake line
poe -C $repo host-check    # construct and bind, then exit
```
