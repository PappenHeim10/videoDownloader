using System.Text.Json;

namespace VideoDownloader.Client;

/// <summary>
/// The one line the core prints on stdout before it goes quiet.
/// </summary>
/// <remarks>
/// Everything after this travels over the socket. The line says where to connect
/// and proves the connection belongs to the process we started: the port is
/// guessable and any local process may reach it, so the token is what
/// distinguishes our core from whatever else happens to hold that port.
/// </remarks>
public sealed record CoreHandshake(int Protocol, int Port, string Token)
{
    /// <summary>The protocol this front end was built against.</summary>
    /// <remarks>
    /// Stated, not negotiated. A core announcing anything else is refused rather
    /// than guessed at - there is exactly one core and one front end and they
    /// ship together, so a version skew is a packaging fault and not a situation
    /// to recover from.
    /// </remarks>
    public const int SupportedProtocol = 1;

    public static CoreHandshake Parse(string line)
    {
        if (string.IsNullOrWhiteSpace(line))
        {
            throw new CoreProtocolException("the core printed no handshake line");
        }

        JsonElement root;
        try
        {
            root = JsonDocument.Parse(line).RootElement;
        }
        catch (JsonException error)
        {
            throw new CoreProtocolException($"the handshake line is not JSON: {error.Message}", error);
        }

        if (root.ValueKind != JsonValueKind.Object)
        {
            throw new CoreProtocolException($"the handshake is not an object but {root.ValueKind}");
        }

        var protocol = root.TryGetProperty("protocol", out var p) && p.TryGetInt32(out var value) ? value : -1;
        var port = root.TryGetProperty("port", out var q) && q.TryGetInt32(out var portValue) ? portValue : -1;
        var token = root.TryGetProperty("token", out var t) && t.ValueKind == JsonValueKind.String
            ? t.GetString()!
            : string.Empty;

        if (protocol != SupportedProtocol)
        {
            throw new CoreProtocolException(
                $"the core speaks protocol {protocol}, this front end speaks {SupportedProtocol}");
        }

        if (port is <= 0 or > 65535)
        {
            throw new CoreProtocolException($"the handshake names no usable port ({port})");
        }

        if (token.Length == 0)
        {
            throw new CoreProtocolException("the handshake carries no token");
        }

        return new CoreHandshake(protocol, port, token);
    }
}
