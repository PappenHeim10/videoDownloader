using Xunit;

namespace VideoDownloader.Client.Tests;

/// <summary>
/// The handshake is the only thing the core says outside the socket, so every
/// way it can be wrong is a way the front end can start talking to the wrong
/// process.
/// </summary>
public sealed class HandshakeTests
{
    [Fact]
    public void Reads_the_one_line_the_core_prints()
    {
        var handshake = CoreHandshake.Parse("""{"protocol":1,"port":54321,"token":"abc123"}""");

        Assert.Equal(1, handshake.Protocol);
        Assert.Equal(54321, handshake.Port);
        Assert.Equal("abc123", handshake.Token);
    }

    [Fact]
    public void Refuses_a_protocol_it_was_not_built_against()
    {
        // Stated, not negotiated: guessing what changed is how a front end ends
        // up half-working against a core it does not understand.
        var error = Assert.Throws<CoreProtocolException>(
            () => CoreHandshake.Parse("""{"protocol":2,"port":1,"token":"t"}"""));

        Assert.Contains("protocol 2", error.Message);
    }

    [Fact]
    public void Refuses_a_line_that_carries_no_token()
    {
        Assert.Throws<CoreProtocolException>(
            () => CoreHandshake.Parse("""{"protocol":1,"port":54321}"""));
    }

    [Theory]
    [InlineData("""{"protocol":1,"port":0,"token":"t"}""")]
    [InlineData("""{"protocol":1,"port":70000,"token":"t"}""")]
    public void Refuses_a_port_nothing_can_listen_on(string line)
    {
        Assert.Throws<CoreProtocolException>(() => CoreHandshake.Parse(line));
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("not json at all")]
    [InlineData("[1,2,3]")]
    public void Refuses_anything_that_is_not_a_handshake(string line)
    {
        Assert.Throws<CoreProtocolException>(() => CoreHandshake.Parse(line));
    }

    [Fact]
    public void A_log_line_on_stdout_would_be_caught_here()
    {
        // The core sends logging to stderr precisely so this cannot happen. If it
        // ever regresses, the front end must refuse rather than hang waiting for
        // a port it never learned.
        Assert.Throws<CoreProtocolException>(
            () => CoreHandshake.Parse("INFO:video_downloader.host.server:Listening on 127.0.0.1:54321"));
    }
}
