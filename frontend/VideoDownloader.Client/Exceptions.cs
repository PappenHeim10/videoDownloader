namespace VideoDownloader.Client;

/// <summary>A line that is not a message this protocol defines.</summary>
public class CoreProtocolException : Exception
{
    public CoreProtocolException(string message) : base(message) { }

    public CoreProtocolException(string message, Exception inner) : base(message, inner) { }
}

/// <summary>The core answered a command with a refusal.</summary>
/// <remarks>
/// Carries the code rather than only the sentence, because the code is what a
/// program reads: <c>unauthenticated</c>, <c>unknown_command</c>,
/// <c>bad_request</c>, <c>not_found</c>, <c>failed</c>.
/// </remarks>
public sealed class CoreCommandException : Exception
{
    public CoreCommandException(string code, string message) : base(message) => Code = code;

    public string Code { get; }

    public override string ToString() => $"{Code}: {Message}";
}

/// <summary>The connection to the core ended while something was waiting on it.</summary>
/// <remarks>
/// Every pending command fails with this rather than waiting for a timeout that
/// would never be answered. The core does the same in the other direction: when
/// the front end disconnects, every open ask fails at once.
/// </remarks>
public sealed class CoreDisconnectedException : Exception
{
    public CoreDisconnectedException(string message, Exception? inner = null) : base(message, inner) { }
}
