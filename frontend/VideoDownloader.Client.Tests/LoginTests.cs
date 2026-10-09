using System.Text.Json.Nodes;
using Xunit;

namespace VideoDownloader.Client.Tests;

/// <summary>
/// A login shown here and decided by the core. The client's part is small and
/// that is the point: report what the page holds, and hear when it is over.
/// </summary>
public sealed class LoginTests
{
    [Fact]
    public async Task The_sites_a_login_can_be_started_for_come_from_the_core()
    {
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        var listing = client.ListSessionsAsync();
        var command = await core.NextOfTypeAsync("sessions.list");
        await core.ReplyAsync(command.GetProperty("id").GetInt64(), new JsonObject
        {
            ["sites"] = new JsonArray(),
            ["persists"] = true,
            ["logins"] = new JsonArray(new JsonObject
            {
                ["site"] = "x.com",
                ["loginUrl"] = "https://x.com/login",
                ["requiredCookies"] = new JsonArray("auth_token", "ct0"),
            }),
        });

        var overview = await listing;
        var login = Assert.Single(overview.Logins);
        Assert.Equal("x.com", login.Site);
        Assert.Equal("https://x.com/login", login.LoginUrl);
        Assert.Equal(["auth_token", "ct0"], login.RequiredCookies);
        Assert.True(overview.Persists);
    }

    [Fact]
    public async Task Starting_a_login_answers_with_the_page_to_show()
    {
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        var starting = client.StartLoginAsync("x.com");
        var command = await core.NextOfTypeAsync("login.start");
        Assert.Equal("x.com", command.GetProperty("site").GetString());
        await core.ReplyAsync(command.GetProperty("id").GetInt64(), new JsonObject
        {
            ["loginId"] = "L1",
            ["site"] = "x.com",
            ["loginUrl"] = "https://x.com/login",
            ["requiredCookies"] = new JsonArray("auth_token", "ct0"),
        });

        var page = await starting;
        Assert.Equal("L1", page.LoginId);
        Assert.Equal("https://x.com/login", page.LoginUrl);
    }

    [Fact]
    public async Task A_snapshot_carries_every_cookie_the_page_holds()
    {
        // Every cookie, not only the required ones: which of them make a session
        // is the core's rule, and filtering here would be a second copy of it.
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        var observing = client.ObserveLoginAsync("L1",
        [
            new SessionCookie("guest_id", "g", ".x.com"),
            new SessionCookie("auth_token", "t", ".x.com"),
        ]);
        var command = await core.NextOfTypeAsync("login.observe");
        await core.ReplyAsync(command.GetProperty("id").GetInt64(), new JsonObject { ["active"] = true });

        Assert.True(await observing);
        Assert.Equal("L1", command.GetProperty("loginId").GetString());
        var cookies = command.GetProperty("cookies");
        Assert.Equal(2, cookies.GetArrayLength());
        Assert.Equal("auth_token", cookies[1].GetProperty("name").GetString());
        Assert.Equal("t", cookies[1].GetProperty("value").GetString());
        Assert.Equal(".x.com", cookies[1].GetProperty("domain").GetString());
    }

    [Fact]
    public async Task A_snapshot_after_the_end_is_answered_not_refused()
    {
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        var observing = client.ObserveLoginAsync("gone", []);
        var command = await core.NextOfTypeAsync("login.observe");
        await core.ReplyAsync(command.GetProperty("id").GetInt64(), new JsonObject { ["active"] = false });

        Assert.False(await observing);
    }

    [Fact]
    public async Task The_end_of_a_login_is_announced()
    {
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        var finished = new TaskCompletionSource<LoginOutcome>(TaskCreationOptions.RunContinuationsAsynchronously);
        client.LoginFinished += outcome => finished.TrySetResult(outcome);
        await client.ConnectAsync(core.Handshake);

        await core.SendAsync(new JsonObject
        {
            ["type"] = "login.finished",
            ["loginId"] = "L1",
            ["site"] = "x.com",
            ["signedIn"] = true,
            ["persisted"] = false,
        });

        var outcome = await finished.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal(new LoginOutcome("L1", "x.com", SignedIn: true, Persisted: false), outcome);
    }

    [Fact]
    public async Task Cancelling_names_the_login()
    {
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        var cancelling = client.CancelLoginAsync("L1");
        var command = await core.NextOfTypeAsync("login.cancel");
        await core.ReplyAsync(command.GetProperty("id").GetInt64(), new JsonObject { ["cancelled"] = true });

        Assert.True(await cancelling);
        Assert.Equal("L1", command.GetProperty("loginId").GetString());
    }
}
