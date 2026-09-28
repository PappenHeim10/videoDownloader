using System.Collections.Concurrent;
using System.Net.Sockets;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace VideoDownloader.Client;

/// <summary>
/// Speaks host protocol version 1 to a core running as a child process.
/// </summary>
/// <remarks>
/// <para>
/// Three properties of this class are not decoration, they are the protocol:
/// </para>
/// <list type="number">
/// <item>
/// <b>One reader, results correlated by id.</b> An event can overtake the result
/// of the command that caused it, so a caller must never read the next line and
/// assume it is the answer. Every line goes through one loop that either
/// completes a waiting command or raises an event.
/// </item>
/// <item>
/// <b>An ask never runs on the reader.</b> The core is blocked on each ask, and a
/// login is answered by somebody typing a password, a two-factor code and
/// possibly an e-mail challenge - minutes, not milliseconds. Handling it inline
/// would stop every download's progress from arriving for exactly that long.
/// So the handler runs on its own task and replies when it is done.
/// </item>
/// <item>
/// <b>Events are raised on the reader's thread.</b> Marshalling them to a UI
/// belongs to whoever has a UI; this assembly has none and must not pretend to.
/// The Avalonia side posts to the dispatcher, a test observes them directly.
/// </item>
/// </list>
/// </remarks>
public sealed class DownloadCoreClient : IAsyncDisposable
{
    private readonly ConcurrentDictionary<long, TaskCompletionSource<JsonElement>> _pending = new();
    private readonly CancellationTokenSource _life = new();
    private readonly SemaphoreSlim _writeLock = new(1, 1);

    private TcpClient? _socket;
    private NdjsonStream? _wire;
    private Task? _reader;
    private long _nextId;
    private int _disposed;

    /// <summary>A job the front end asked for has been created.</summary>
    /// <remarks>
    /// Jobs that already existed when this client connected arrive in the
    /// <see cref="ConnectAsync"/> result instead, not as events.
    /// </remarks>
    public event Action<JobSnapshot>? JobCreated;

    /// <summary>A job changed. Already coalesced by the core to at most one per 0.15 s.</summary>
    public event Action<JobSnapshot>? JobChanged;

    /// <summary>A job is gone, by id.</summary>
    public event Action<string>? JobRemoved;

    /// <summary>The connection ended - with the reason, or null for an orderly close.</summary>
    public event Action<Exception?>? Closed;

    /// <summary>Answers the core's large-download question. Null means nobody is there to ask.</summary>
    public Func<LargeDownloadAsk, CancellationToken, Task<bool>>? ConfirmLargeDownload { get; set; }

    /// <summary>
    /// Answers the core's login request with the collected cookies, or null when
    /// the user cancelled.
    /// </summary>
    public Func<LoginAsk, CancellationToken, Task<IReadOnlyList<SessionCookie>?>>? RequestLogin { get; set; }

    public bool IsConnected => _reader is { IsCompleted: false };

    /// <summary>Connect, say hello, and take the job list the core answers with.</summary>
    public async Task<IReadOnlyList<JobSnapshot>> ConnectAsync(
        CoreHandshake handshake, CancellationToken cancellationToken = default)
    {
        if (_socket is not null)
        {
            throw new InvalidOperationException("this client is already connected");
        }

        _socket = new TcpClient();
        await _socket.ConnectAsync("127.0.0.1", handshake.Port, cancellationToken).ConfigureAwait(false);
        _wire = new NdjsonStream(_socket.GetStream());
        _reader = Task.Run(() => ReadLoopAsync(_life.Token), CancellationToken.None);

        var hello = new JsonObject { ["token"] = handshake.Token };
        var data = await SendCommandAsync("hello", hello, cancellationToken).ConfigureAwait(false);

        // The core states its version here too. It agreed with the handshake or
        // we would not have got this far, but a mismatch now would mean the line
        // on stdout and the process on the socket are not the same core.
        if (data.TryGetProperty("protocol", out var spoken)
            && spoken.TryGetInt32(out var version)
            && version != CoreHandshake.SupportedProtocol)
        {
            throw new CoreProtocolException(
                $"the socket speaks protocol {version}, the handshake said {handshake.Protocol}");
        }

        return ReadJobs(data);
    }

    // --- commands -----------------------------------------------------------

    public async Task<IReadOnlyList<JobSnapshot>> ListJobsAsync(CancellationToken cancellationToken = default) =>
        ReadJobs(await SendCommandAsync("jobs.list", new JsonObject(), cancellationToken).ConfigureAwait(false));

    public async Task<JobSnapshot> AddJobAsync(
        string url, string? quality = null, string? outputDirectory = null,
        CancellationToken cancellationToken = default)
    {
        var payload = new JsonObject { ["url"] = url };
        if (quality is not null)
        {
            payload["quality"] = quality;
        }

        if (outputDirectory is not null)
        {
            payload["outputDir"] = outputDirectory;
        }

        var data = await SendCommandAsync("jobs.add", payload, cancellationToken).ConfigureAwait(false);
        return ReadJob(data.GetProperty("job"));
    }

    public async Task<JobSnapshot> CancelJobAsync(string jobId, CancellationToken cancellationToken = default)
    {
        var payload = new JsonObject { ["jobId"] = jobId };
        var data = await SendCommandAsync("jobs.cancel", payload, cancellationToken).ConfigureAwait(false);
        return ReadJob(data.GetProperty("job"));
    }

    /// <summary>Remove a job, and say explicitly whether its file goes with it.</summary>
    /// <param name="deleteFile">
    /// Required, with no default, and that is the point. For an entry the core
    /// found by scanning the output directory, deleting removes a real video file
    /// that nobody downloaded in this session - so the decision is never implied.
    /// </param>
    public Task DeleteJobAsync(string jobId, bool deleteFile, CancellationToken cancellationToken = default)
    {
        var payload = new JsonObject { ["jobId"] = jobId, ["deleteFile"] = deleteFile };
        return SendCommandAsync("jobs.delete", payload, cancellationToken);
    }

    public async Task<IReadOnlyList<JobSnapshot>> RescanAsync(CancellationToken cancellationToken = default) =>
        ReadJobs(await SendCommandAsync("jobs.rescan", new JsonObject(), cancellationToken).ConfigureAwait(false));

    public async Task<string?> GetDownloadDirectoryAsync(CancellationToken cancellationToken = default)
    {
        var data = await SendCommandAsync("settings.get", new JsonObject(), cancellationToken).ConfigureAwait(false);
        return ReadNullableString(data, "downloadDirectory");
    }

    public async Task<string?> SetDownloadDirectoryAsync(string path, CancellationToken cancellationToken = default)
    {
        var payload = new JsonObject { ["path"] = path };
        var data = await SendCommandAsync("settings.setDownloadDirectory", payload, cancellationToken)
            .ConfigureAwait(false);
        return ReadNullableString(data, "downloadDirectory");
    }

    public async Task<SessionOverview> ListSessionsAsync(CancellationToken cancellationToken = default)
    {
        var data = await SendCommandAsync("sessions.list", new JsonObject(), cancellationToken).ConfigureAwait(false);
        var sites = new List<string>();
        if (data.TryGetProperty("sites", out var array) && array.ValueKind == JsonValueKind.Array)
        {
            sites.AddRange(array.EnumerateArray().Select(site => site.GetString() ?? string.Empty));
        }

        var persists = data.TryGetProperty("persists", out var flag) && flag.ValueKind == JsonValueKind.True;
        return new SessionOverview(sites, persists);
    }

    public async Task<bool> PutSessionAsync(
        string site, IReadOnlyList<SessionCookie> cookies, CancellationToken cancellationToken = default)
    {
        var payload = new JsonObject { ["site"] = site, ["cookies"] = CookiesToJson(cookies) };
        var data = await SendCommandAsync("sessions.put", payload, cancellationToken).ConfigureAwait(false);
        return data.TryGetProperty("persisted", out var flag) && flag.ValueKind == JsonValueKind.True;
    }

    public async Task<bool> ClearSessionAsync(string site, CancellationToken cancellationToken = default)
    {
        var payload = new JsonObject { ["site"] = site };
        var data = await SendCommandAsync("sessions.clear", payload, cancellationToken).ConfigureAwait(false);
        return data.TryGetProperty("removed", out var flag) && flag.ValueKind == JsonValueKind.True;
    }

    public Task ShutdownAsync(CancellationToken cancellationToken = default) =>
        SendCommandAsync("app.shutdown", new JsonObject(), cancellationToken);

    // --- the wire -----------------------------------------------------------

    private async Task<JsonElement> SendCommandAsync(
        string type, JsonObject payload, CancellationToken cancellationToken)
    {
        if (_wire is null)
        {
            throw new InvalidOperationException("connect before sending commands");
        }

        var id = Interlocked.Increment(ref _nextId);
        payload["type"] = type;
        payload["id"] = id;

        var waiter = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = waiter;

        try
        {
            await WriteAsync(payload, cancellationToken).ConfigureAwait(false);
            await using var _ = cancellationToken.Register(
                () => waiter.TrySetCanceled(cancellationToken)).ConfigureAwait(false);
            return await waiter.Task.ConfigureAwait(false);
        }
        finally
        {
            _pending.TryRemove(id, out _);
        }
    }

    private async Task WriteAsync(JsonNode message, CancellationToken cancellationToken)
    {
        await _writeLock.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await _wire!.WriteLineAsync(message.ToJsonString(), cancellationToken).ConfigureAwait(false);
        }
        catch (Exception error) when (error is IOException or ObjectDisposedException or SocketException)
        {
            throw new CoreDisconnectedException("the connection to the core is gone", error);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    private async Task ReadLoopAsync(CancellationToken cancellationToken)
    {
        Exception? reason = null;
        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                var line = await _wire!.ReadLineAsync(cancellationToken).ConfigureAwait(false);
                if (line is null)
                {
                    break;
                }

                if (line.Length == 0)
                {
                    continue;
                }

                Dispatch(line);
            }
        }
        catch (OperationCanceledException)
        {
            // Disposing is not a failure.
        }
        catch (Exception error)
        {
            reason = error;
        }
        finally
        {
            FailPending(reason);
            Closed?.Invoke(reason);
        }
    }

    private void Dispatch(string line)
    {
        JsonElement message;
        try
        {
            message = JsonDocument.Parse(line).RootElement;
        }
        catch (JsonException)
        {
            // A core that sends an unreadable line is a defect, but dropping one
            // line keeps every job already on screen alive. Closing would not.
            return;
        }

        if (message.ValueKind != JsonValueKind.Object
            || !message.TryGetProperty("type", out var typeNode)
            || typeNode.ValueKind != JsonValueKind.String)
        {
            return;
        }

        // One message must never be able to end the connection - not a payload
        // shaped differently than expected, and not a subscriber that throws.
        // The core follows the same rule in the other direction, where a failing
        // listener is caught per listener rather than taken as a reason to stop
        // notifying the rest.
        try
        {
            switch (typeNode.GetString())
            {
                case "result":
                    CompleteResult(message);
                    break;

                case "job.created":
                    if (message.TryGetProperty("job", out var created))
                    {
                        JobCreated?.Invoke(ReadJob(created));
                    }

                    break;

                case "job.changed":
                    if (message.TryGetProperty("job", out var changed))
                    {
                        JobChanged?.Invoke(ReadJob(changed));
                    }

                    break;

                case "job.removed":
                    JobRemoved?.Invoke(ReadNullableString(message, "jobId") ?? string.Empty);
                    break;

                case "ask.confirmLargeDownload":
                    StartAsk(ReadLargeDownloadAsk(message));
                    break;

                case "ask.login":
                    StartAsk(ReadLoginAsk(message));
                    break;
            }
        }
        catch (Exception)
        {
            // Deliberately swallowed. Losing one update is a cosmetic fault that
            // the next one repairs; losing the reader would silently freeze every
            // job on screen while the downloads carried on behind it.
        }
    }

    private void CompleteResult(JsonElement message)
    {
        if (!message.TryGetProperty("id", out var idNode)
            || idNode.ValueKind != JsonValueKind.Number
            || !idNode.TryGetInt64(out var id))
        {
            // A result without an id answers nothing - the core sends one for a
            // line it could not read at all, and there is no caller waiting.
            return;
        }

        if (!_pending.TryRemove(id, out var waiter))
        {
            return;
        }

        var ok = message.TryGetProperty("ok", out var okNode) && okNode.ValueKind == JsonValueKind.True;
        if (ok)
        {
            var data = message.TryGetProperty("data", out var payload) ? payload : default;
            waiter.TrySetResult(data);
            return;
        }

        var code = "failed";
        var text = "the core refused the command";
        if (message.TryGetProperty("error", out var error) && error.ValueKind == JsonValueKind.Object)
        {
            code = error.TryGetProperty("code", out var c) ? c.GetString() ?? code : code;
            text = error.TryGetProperty("message", out var m) ? m.GetString() ?? text : text;
        }

        waiter.TrySetException(new CoreCommandException(code, text));
    }

    /// <summary>
    /// Answer an ask on its own task, so a question that takes minutes does not
    /// stop everything else on this connection for that long.
    /// </summary>
    private void StartAsk(object ask)
    {
        _ = Task.Run(async () =>
        {
            var askId = ask switch
            {
                LargeDownloadAsk large => large.AskId,
                LoginAsk login => login.AskId,
                _ => string.Empty,
            };

            JsonNode? value;
            try
            {
                value = ask switch
                {
                    // No handler means nobody is there to ask, and the conservative
                    // answer is the refusal - the same answer the core takes when
                    // the front end is gone entirely.
                    LargeDownloadAsk large => ConfirmLargeDownload is null
                        ? JsonValue.Create(false)
                        : JsonValue.Create(await ConfirmLargeDownload(large, _life.Token).ConfigureAwait(false)),
                    LoginAsk login => RequestLogin is null
                        ? null
                        : LoginReply(await RequestLogin(login, _life.Token).ConfigureAwait(false)),
                    _ => null,
                };
            }
            catch (Exception)
            {
                // A handler that threw has not answered. Saying "no" is the only
                // safe reading: starting several gigabytes, or storing a session
                // that was never completed, are both worse than a refusal.
                value = ask is LargeDownloadAsk ? JsonValue.Create(false) : null;
            }

            var reply = new JsonObject { ["type"] = "ask.reply", ["askId"] = askId, ["value"] = value };
            try
            {
                await WriteAsync(reply, _life.Token).ConfigureAwait(false);
            }
            catch (Exception)
            {
                // The connection went while we were asking. The core fails its own
                // pending asks when that happens, so there is nothing left to tell.
            }
        }, CancellationToken.None);
    }

    private static JsonNode? LoginReply(IReadOnlyList<SessionCookie>? cookies) =>
        cookies is null ? null : new JsonObject { ["cookies"] = CookiesToJson(cookies) };

    private static JsonArray CookiesToJson(IReadOnlyList<SessionCookie> cookies)
    {
        var array = new JsonArray();
        foreach (var cookie in cookies)
        {
            array.Add(new JsonObject
            {
                ["name"] = cookie.Name,
                ["value"] = cookie.Value,
                ["domain"] = cookie.Domain,
            });
        }

        return array;
    }

    private void FailPending(Exception? reason)
    {
        var failure = new CoreDisconnectedException("the connection to the core ended", reason);
        foreach (var id in _pending.Keys)
        {
            if (_pending.TryRemove(id, out var waiter))
            {
                waiter.TrySetException(failure);
            }
        }
    }

    // --- reading payloads ---------------------------------------------------

    private static IReadOnlyList<JobSnapshot> ReadJobs(JsonElement data)
    {
        if (data.ValueKind != JsonValueKind.Object
            || !data.TryGetProperty("jobs", out var jobs)
            || jobs.ValueKind != JsonValueKind.Array)
        {
            return [];
        }

        return jobs.EnumerateArray().Select(ReadJob).ToList();
    }

    private static JobSnapshot ReadJob(JsonElement job) => new(
        Id: job.GetProperty("id").GetString() ?? string.Empty,
        Url: ReadNullableString(job, "url"),
        Title: ReadNullableString(job, "title"),
        State: ReadNullableString(job, "state") ?? "created",
        Progress: ReadDouble(job, "progress"),
        Done: ReadLong(job, "done"),
        Total: ReadLong(job, "total"),
        Unit: ReadNullableString(job, "unit") ?? "bytes",
        HasKnownTotal: job.TryGetProperty("hasKnownTotal", out var known) && known.ValueKind == JsonValueKind.True,
        OutputFile: ReadNullableString(job, "outputFile"),
        ExpectedBytes: ReadNullableLong(job, "expectedBytes"),
        Error: ReadFailure(job),
        FromDisk: job.TryGetProperty("fromDisk", out var disk) && disk.ValueKind == JsonValueKind.True,
        Seq: ReadLong(job, "seq"));

    private static JobFailure? ReadFailure(JsonElement job)
    {
        if (!job.TryGetProperty("error", out var error) || error.ValueKind != JsonValueKind.Object)
        {
            return null;
        }

        return new JobFailure(
            Kind: ReadNullableString(error, "kind") ?? "unknown",
            Code: ReadNullableString(error, "code") ?? string.Empty,
            Message: ReadNullableString(error, "message") ?? string.Empty,
            Retryable: error.TryGetProperty("retryable", out var r) && r.ValueKind == JsonValueKind.True,
            Text: ReadNullableString(error, "text") ?? string.Empty);
    }

    private static LargeDownloadAsk ReadLargeDownloadAsk(JsonElement message) => new(
        AskId: ReadNullableString(message, "askId") ?? string.Empty,
        JobId: ReadNullableString(message, "jobId") ?? string.Empty,
        Title: ReadNullableString(message, "title"),
        EstimatedBytes: ReadNullableLong(message, "estimatedBytes"));

    private static LoginAsk ReadLoginAsk(JsonElement message)
    {
        var required = new List<string>();
        if (message.TryGetProperty("requiredCookies", out var array) && array.ValueKind == JsonValueKind.Array)
        {
            required.AddRange(array.EnumerateArray().Select(name => name.GetString() ?? string.Empty));
        }

        return new LoginAsk(
            AskId: ReadNullableString(message, "askId") ?? string.Empty,
            Site: ReadNullableString(message, "site") ?? string.Empty,
            LoginUrl: ReadNullableString(message, "loginUrl") ?? string.Empty,
            RequiredCookies: required);
    }

    private static string? ReadNullableString(JsonElement element, string name) =>
        element.ValueKind == JsonValueKind.Object
        && element.TryGetProperty(name, out var value)
        && value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : null;

    /// <summary>
    /// A number, or zero when the field is absent or null.
    /// </summary>
    /// <remarks>
    /// The <c>ValueKind</c> check is not belt and braces: <see cref="JsonElement.TryGetInt64"/>
    /// <b>throws</b> on a null element rather than returning false. The core sends
    /// <c>expectedBytes: null</c> whenever a provider states no size, so leaving
    /// it out turns an ordinary job into an exception - and, before this was
    /// caught in <see cref="Dispatch"/>, into a dead connection.
    /// </remarks>
    private static long ReadLong(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value)
        && value.ValueKind == JsonValueKind.Number
        && value.TryGetInt64(out var number)
            ? number
            : 0;

    private static long? ReadNullableLong(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value)
        && value.ValueKind == JsonValueKind.Number
        && value.TryGetInt64(out var number)
            ? number
            : null;

    private static double ReadDouble(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value)
        && value.ValueKind == JsonValueKind.Number
        && value.TryGetDouble(out var number)
            ? number
            : 0;

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) == 1)
        {
            return;
        }

        await _life.CancelAsync().ConfigureAwait(false);
        _socket?.Close();

        if (_reader is not null)
        {
            try
            {
                await _reader.ConfigureAwait(false);
            }
            catch (Exception)
            {
                // The reader ending badly is what disposing was for.
            }
        }

        _wire?.Dispose();
        _socket?.Dispose();
        _life.Dispose();
        _writeLock.Dispose();
    }
}
