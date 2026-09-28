using System.Text.Json;
using System.Text.Json.Nodes;
using Xunit;

namespace VideoDownloader.Client.Tests;

/// <summary>
/// The two questions the core cannot answer itself. The core is blocked on each
/// of them, which makes every one of these tests about what happens to everything
/// else while somebody is being asked.
/// </summary>
public sealed class AskTests
{
    [Fact]
    public async Task A_question_that_takes_minutes_does_not_stop_the_connection()
    {
        // This is the whole reason asks run on their own task. A login is answered
        // by somebody typing a password, a two-factor code and possibly an e-mail
        // challenge. Handling it on the reader would freeze every download's
        // progress for exactly that long - and the freeze would look like a hang,
        // not like a question.
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();

        var asked = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        client.ConfirmLargeDownload = (_, _) =>
        {
            asked.TrySetResult();
            return release.Task;
        };

        await client.ConnectAsync(core.Handshake);

        await core.SendAsync(new JsonObject
        {
            ["type"] = "ask.confirmLargeDownload",
            ["askId"] = "q1",
            ["jobId"] = "a",
            ["title"] = "A large one",
            ["estimatedBytes"] = 3221225472,
        });
        await asked.Task.WaitAsync(TimeSpan.FromSeconds(5));

        // The question is still open. An event must still get through.
        var seen = new TaskCompletionSource<JobSnapshot>(TaskCreationOptions.RunContinuationsAsynchronously);
        client.JobChanged += snapshot => seen.TrySetResult(snapshot);
        await core.SendAsync(new JsonObject
        {
            ["type"] = "job.changed",
            ["job"] = new JsonObject
            {
                ["id"] = "b",
                ["state"] = "downloading",
                ["progress"] = 10.0,
                ["done"] = 1,
                ["total"] = 10,
                ["unit"] = "segments",
                ["hasKnownTotal"] = true,
                ["seq"] = 2,
            },
        });

        var job = await seen.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal("b", job.Id);

        release.TrySetResult(true);
        var reply = await core.NextOfTypeAsync("ask.reply");
        Assert.Equal("q1", reply.GetProperty("askId").GetString());
        Assert.True(reply.GetProperty("value").GetBoolean());
    }

    [Fact]
    public async Task A_size_question_nobody_can_answer_is_refused()
    {
        // No handler means nobody is there to ask, and the conservative answer is
        // the only safe one. Starting several gigabytes because the window that
        // was meant to ask is gone is the failure the question exists to prevent.
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        await core.SendAsync(new JsonObject
        {
            ["type"] = "ask.confirmLargeDownload",
            ["askId"] = "q1",
            ["jobId"] = "a",
            ["estimatedBytes"] = 3221225472,
        });

        var reply = await core.NextOfTypeAsync("ask.reply");
        Assert.False(reply.GetProperty("value").GetBoolean());
    }

    [Fact]
    public async Task A_handler_that_throws_still_answers_no()
    {
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        client.ConfirmLargeDownload = (_, _) => throw new InvalidOperationException("the dialog blew up");

        await client.ConnectAsync(core.Handshake);
        await core.SendAsync(new JsonObject
        {
            ["type"] = "ask.confirmLargeDownload",
            ["askId"] = "q1",
            ["jobId"] = "a",
            ["estimatedBytes"] = 1,
        });

        // A handler that threw has not answered. A job blocked forever because a
        // dialog failed would be worse than a refused download.
        var reply = await core.NextOfTypeAsync("ask.reply");
        Assert.False(reply.GetProperty("value").GetBoolean());
    }

    [Fact]
    public async Task The_size_question_arrives_with_what_it_needs_to_be_asked()
    {
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();

        LargeDownloadAsk? received = null;
        var asked = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        client.ConfirmLargeDownload = (ask, _) =>
        {
            received = ask;
            asked.TrySetResult();
            return Task.FromResult(true);
        };

        await client.ConnectAsync(core.Handshake);
        await core.SendAsync(new JsonObject
        {
            ["type"] = "ask.confirmLargeDownload",
            ["askId"] = "q1",
            ["jobId"] = "job-7",
            ["title"] = "Ein grosses Video",
            ["estimatedBytes"] = 3221225472,
        });
        await asked.Task.WaitAsync(TimeSpan.FromSeconds(5));

        Assert.Equal("job-7", received!.JobId);
        Assert.Equal("Ein grosses Video", received.Title);
        Assert.Equal(3221225472, received.EstimatedBytes);
    }

    [Fact]
    public async Task A_finished_login_hands_the_cookies_back()
    {
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();

        LoginAsk? received = null;
        client.RequestLogin = (ask, _) =>
        {
            received = ask;
            return Task.FromResult<IReadOnlyList<SessionCookie>?>(
            [
                new SessionCookie("auth_token", "secret-one", ".x.com"),
                new SessionCookie("ct0", "secret-two", ".x.com"),
            ]);
        };

        await client.ConnectAsync(core.Handshake);
        await core.SendAsync(new JsonObject
        {
            ["type"] = "ask.login",
            ["askId"] = "q2",
            ["site"] = "x.com",
            ["loginUrl"] = "https://x.com/login",
            ["requiredCookies"] = new JsonArray("auth_token", "ct0"),
        });

        var reply = await core.NextOfTypeAsync("ask.reply");
        var cookies = reply.GetProperty("value").GetProperty("cookies");

        Assert.Equal(["auth_token", "ct0"], received!.RequiredCookies);
        Assert.Equal(2, cookies.GetArrayLength());
        Assert.Equal("auth_token", cookies[0].GetProperty("name").GetString());
        Assert.Equal(".x.com", cookies[0].GetProperty("domain").GetString());
    }

    [Fact]
    public async Task A_cancelled_login_says_nothing_rather_than_half_a_session()
    {
        // Null is the answer for "the user closed the window". A half session is
        // not one, and the core must never have to check whether what it got adds
        // up to a login.
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        client.RequestLogin = (_, _) => Task.FromResult<IReadOnlyList<SessionCookie>?>(null);

        await client.ConnectAsync(core.Handshake);
        await core.SendAsync(new JsonObject
        {
            ["type"] = "ask.login",
            ["askId"] = "q2",
            ["site"] = "x.com",
            ["loginUrl"] = "https://x.com/login",
            ["requiredCookies"] = new JsonArray("auth_token", "ct0"),
        });

        var reply = await core.NextOfTypeAsync("ask.reply");
        Assert.Equal(JsonValueKind.Null, reply.GetProperty("value").ValueKind);
    }

    [Fact]
    public void A_cookie_never_prints_its_value()
    {
        // A value here is an account. The Python side writes its repr by hand for
        // the same reason: one exception carrying a session into a log is enough.
        var cookie = new SessionCookie("auth_token", "the-actual-secret", ".x.com");

        var printed = cookie.ToString();

        Assert.DoesNotContain("the-actual-secret", printed, StringComparison.Ordinal);
        Assert.Contains("auth_token", printed, StringComparison.Ordinal);
        Assert.Contains("redacted", printed, StringComparison.Ordinal);
    }
}
