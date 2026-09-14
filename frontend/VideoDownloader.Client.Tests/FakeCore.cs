using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Channels;

namespace VideoDownloader.Client.Tests;

/// <summary>
/// A core, as far as the wire is concerned: a loopback listener that speaks
/// NDJSON and does exactly what a test tells it to.
/// </summary>
/// <remarks>
/// This is the whole reason the protocol was worth writing down. The real core
/// needs Python, a provider registry and an event loop; the contract needs a
/// socket and a line format. A front end held to the contract by this can be
/// wrong about the core's behaviour, but never about its protocol - and the
/// core is held to the same contract from its own side.
/// </remarks>
internal sealed class FakeCore : IAsyncDisposable
{
    private static readonly UTF8Encoding Utf8 = new(false);

    private readonly TcpListener _listener;
    private readonly Channel<JsonElement> _received = Channel.CreateUnbounded<JsonElement>();
    private readonly CancellationTokenSource _life = new();
    private readonly Task _accepting;
    private readonly TaskCompletionSource _connected = new(TaskCreationOptions.RunContinuationsAsynchronously);

    private TcpClient? _client;
    private NetworkStream? _stream;

    public FakeCore(string token = "0123456789abcdef0123456789abcdef")
    {
        _listener = new TcpListener(IPAddress.Loopback, 0);
        _listener.Start();
        var port = ((IPEndPoint)_listener.LocalEndpoint).Port;
        Handshake = new CoreHandshake(1, port, token);
        Token = token;
        _accepting = Task.Run(AcceptAsync);
    }

    public CoreHandshake Handshake { get; }

    public string Token { get; }

    /// <summary>Jobs answered in the hello result. Set before the client connects.</summary>
    public JsonArray HelloJobs { get; set; } = [];

    /// <summary>The next message the client sent, waiting for it if need be.</summary>
    public async Task<JsonElement> NextAsync(TimeSpan? within = null)
    {
        using var timeout = new CancellationTokenSource(within ?? TimeSpan.FromSeconds(5));
        return await _received.Reader.ReadAsync(timeout.Token);
    }

    /// <summary>The next message of this type, skipping anything before it.</summary>
    public async Task<JsonElement> NextOfTypeAsync(string type, TimeSpan? within = null)
    {
        var deadline = DateTime.UtcNow + (within ?? TimeSpan.FromSeconds(5));
        while (DateTime.UtcNow < deadline)
        {
            var message = await NextAsync(deadline - DateTime.UtcNow);
            if (message.GetProperty("type").GetString() == type)
            {
                return message;
            }
        }

        throw new TimeoutException($"no {type} arrived");
    }

    public async Task SendAsync(JsonNode message)
    {
        await _connected.Task.WaitAsync(TimeSpan.FromSeconds(5));
        var bytes = Utf8.GetBytes(message.ToJsonString() + "\n");
        await _stream!.WriteAsync(bytes);
        await _stream.FlushAsync();
    }

    /// <summary>A successful answer to the command with this id.</summary>
    public Task ReplyAsync(long id, JsonObject? data = null) =>
        SendAsync(new JsonObject { ["type"] = "result", ["id"] = id, ["ok"] = true, ["data"] = data ?? [] });

    /// <summary>A refusal, by code.</summary>
    public Task RefuseAsync(long id, string code, string message) =>
        SendAsync(new JsonObject
        {
            ["type"] = "result",
            ["id"] = id,
            ["ok"] = false,
            ["error"] = new JsonObject { ["code"] = code, ["message"] = message },
        });

    /// <summary>Drop the connection the way a crashed core would.</summary>
    public void Disconnect()
    {
        _client?.Close();
        _client = null;
        _stream = null;
    }

    private async Task AcceptAsync()
    {
        try
        {
            _client = await _listener.AcceptTcpClientAsync(_life.Token);
            _stream = _client.GetStream();
            _connected.TrySetResult();
            await ReadAsync(_life.Token);
        }
        catch (Exception)
        {
            // A test that ends mid-conversation is the normal case here.
        }
    }

    private async Task ReadAsync(CancellationToken cancellationToken)
    {
        var buffer = new byte[16 * 1024];
        var pending = new MemoryStream();

        while (!cancellationToken.IsCancellationRequested)
        {
            var read = await _stream!.ReadAsync(buffer, cancellationToken);
            if (read == 0)
            {
                return;
            }

            pending.Write(buffer, 0, read);
            var text = Utf8.GetString(pending.GetBuffer(), 0, (int)pending.Length);
            var lines = text.Split('\n');
            pending.SetLength(0);

            // The last piece is whatever has no newline yet.
            pending.Write(Utf8.GetBytes(lines[^1]));

            foreach (var line in lines[..^1])
            {
                if (line.Trim().Length == 0)
                {
                    continue;
                }

                var message = JsonDocument.Parse(line).RootElement.Clone();
                await _received.Writer.WriteAsync(message, cancellationToken);

                // hello is answered here so every test does not have to.
                if (message.GetProperty("type").GetString() == "hello"
                    && message.TryGetProperty("token", out var token)
                    && token.GetString() == Token)
                {
                    await ReplyAsync(
                        message.GetProperty("id").GetInt64(),
                        new JsonObject { ["protocol"] = 1, ["jobs"] = HelloJobs.DeepClone() });
                }
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        await _life.CancelAsync();
        _client?.Dispose();
        _listener.Stop();
        try
        {
            await _accepting;
        }
        catch (Exception)
        {
            // Already ending.
        }

        _life.Dispose();
    }
}
