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
| `sessions.list` | — | `sites[]`, `persists`, `logins[]` |
| `sessions.put` | `site`, `cookies[]` | `persisted` |
| `sessions.clear` | `site` | `removed` |
| `login.start` | `site` | `loginId`, `site`, `loginUrl`, `requiredCookies[]` |
| `login.observe` | `loginId`, `cookies[]` | `active` |
| `login.cancel` | `loginId` | `cancelled` |
| `app.shutdown` | — | — |
| `ask.reply` | `askId`, `value` | *(no result)* |

`logins[]` in `sessions.list` names every site a login can be started for,
as `{site, loginUrl, requiredCookies}`. A front end builds its sign-in menu
from it and never names a site itself.

`deleteFile` has no default and a missing one is `bad_request`. For an entry
found by a directory scan, deleting removes a real video file that nobody
downloaded in this session, so the decision is never implied.

## Events

```json
{"type":"job.created","job":{…}}
{"type":"job.changed","job":{…}}
{"type":"job.removed","jobId":"…"}
{"type":"login.finished","loginId":"…","site":"x.com","signedIn":true,"persisted":true}
```

`login.finished` ends a login the front end is showing, and it is the only
way one ends from the core's side: `signedIn` is `true` once the session is
stored, `false` when the core gave up on it (a job's login that timed out).

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
* for the login, the only answer is `value: null`, when the user cancelled.
  A login is not answered with cookies - see *Logins* below. `ask.login`
  carries a `loginId` equal to its `askId`, and the core resolves the
  question itself when the login settles.

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

## Logins

A login is shown by the front end and decided by the core. The front end opens
the site's own page in an embedded browser and reports what cookies the page
holds; the core decides when they add up to a session and stores it. A front
end never touches the session store and cannot store a session for a site it
was not asked about.

There are two ways a login starts, and from then on they are the same:

* a job needs one - the core sends `ask.login` with a `loginId`;
* the user asks for one - the front end sends `login.start` for one of the
  sites in `sessions.list`, and the result carries the `loginId`.

While the page is open, the front end sends `login.observe` with every cookie
the page holds, as `{name, value, domain}`, every 250 ms. Each one is a
snapshot, not a delta: a required name that is missing from it counts as
withdrawn. A snapshot for a login that has already ended answers
`active: false` rather than an error, because a timer-driven front end will
send one after the end now and then.

The core applies the same rule the Qt window applied:

* only the site's `requiredCookies` are kept;
* the last value of a name wins;
* once every required cookie is present and non-empty, the core waits 2 s for
  rotated values and then stores the freshest ones. A cookie withdrawn during
  the wait restarts it.

Then it sends `login.finished` and, for a job's login, resolves the ask. The
front end closes the page on `login.finished`, or sends `login.cancel` when the
user closes it first. Every open login ends when the front end disconnects.

Why snapshots and not one event per cookie: the embedded browser the Avalonia
front end uses has no cookie events on any platform, only a pull API. The rule
does not need events - it needs to observe the cookie state more often than its
2 s settle period, and 250 ms is eight observations inside it.

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
