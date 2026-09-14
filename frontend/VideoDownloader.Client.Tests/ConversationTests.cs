using System.Text.Json.Nodes;
using Xunit;

namespace VideoDownloader.Client.Tests;

/// <summary>Commands, results and events, against a core that is not Python.</summary>
public sealed class ConversationTests
{
    private static JsonObject Job(string id, string state = "downloading", bool hasKnownTotal = true) => new()
    {
        ["id"] = id,
        ["url"] = $"https://example.test/{id}",
        ["title"] = $"Video {id}",
        ["state"] = state,
        ["progress"] = 42.5,
        ["done"] = 21,
        ["total"] = hasKnownTotal ? 50 : 0,
        ["unit"] = "segments",
        ["hasKnownTotal"] = hasKnownTotal,
        ["outputFile"] = null,
        ["expectedBytes"] = null,
        ["error"] = null,
        ["fromDisk"] = false,
        ["seq"] = 7,
    };

    [Fact]
    public async Task Says_hello_first_and_proves_who_it_is()
    {
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();

        await client.ConnectAsync(core.Handshake);

        var hello = await core.NextAsync();
        Assert.Equal("hello", hello.GetProperty("type").GetString());
        Assert.Equal(core.Token, hello.GetProperty("token").GetString());
    }

    [Fact]
    public async Task Takes_the_jobs_that_already_existed_from_the_hello_result()
    {
        // Jobs present before the front end connected arrive here, not as
        // job.created events - otherwise a reconnect would look like a burst of
        // new downloads.
        await using var core = new FakeCore { HelloJobs = [Job("a"), Job("b")] };
        await using var client = new DownloadCoreClient();

        var jobs = await client.ConnectAsync(core.Handshake);

        Assert.Equal(["a", "b"], jobs.Select(job => job.Id));
        Assert.Equal("Video a", jobs[0].Title);
    }

    [Fact]
    public async Task A_result_is_found_by_its_id_even_when_an_event_overtakes_it()
    {
        // The protocol says outright that an event can arrive before the result
        // of the command that caused it. A front end that read the next line and
        // called it the answer would be wrong exactly here.
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        var seen = new TaskCompletionSource<JobSnapshot>(TaskCreationOptions.RunContinuationsAsynchronously);
        client.JobCreated += snapshot => seen.TrySetResult(snapshot);

        var pending = client.AddJobAsync("https://example.test/new");
        var command = await core.NextOfTypeAsync("jobs.add");
        var id = command.GetProperty("id").GetInt64();

        // The event first, deliberately, then the answer.
        await core.SendAsync(new JsonObject { ["type"] = "job.created", ["job"] = Job("new") });
        await core.ReplyAsync(id, new JsonObject { ["job"] = Job("new") });

        var created = await seen.Task.WaitAsync(TimeSpan.FromSeconds(5));
        var answered = await pending.WaitAsync(TimeSpan.FromSeconds(5));

        Assert.Equal("new", created.Id);
        Assert.Equal("new", answered.Id);
    }

    [Fact]
    public async Task A_refusal_keeps_its_code()
    {
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        var pending = client.CancelJobAsync("gone");
        var command = await core.NextOfTypeAsync("jobs.cancel");
        await core.RefuseAsync(command.GetProperty("id").GetInt64(), "not_found", "no such job");

        var error = await Assert.ThrowsAsync<CoreCommandException>(() => pending);
        Assert.Equal("not_found", error.Code);
        Assert.Equal("no such job", error.Message);
    }

    [Fact]
    public async Task Deleting_always_states_whether_the_file_goes_too()
    {
        // The core makes deleteFile mandatory because for a job it found by
        // scanning the directory, "delete" means a real video file nobody
        // downloaded in this session. The client must never leave it implied.
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        var pending = client.DeleteJobAsync("a", deleteFile: false);
        var command = await core.NextOfTypeAsync("jobs.delete");

        Assert.True(command.TryGetProperty("deleteFile", out var flag));
        Assert.False(flag.GetBoolean());

        await core.ReplyAsync(command.GetProperty("id").GetInt64());
        await pending.WaitAsync(TimeSpan.FromSeconds(5));
    }

    [Fact]
    public async Task An_unknown_total_does_not_read_as_a_finished_job()
    {
        // hasKnownTotal false means the end is unknown, not that nothing is
        // happening. Reading it as zero progress is the bug that made a finished
        // download show an empty bar.
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        var seen = new TaskCompletionSource<JobSnapshot>(TaskCreationOptions.RunContinuationsAsynchronously);
        client.JobChanged += snapshot => seen.TrySetResult(snapshot);

        await core.SendAsync(new JsonObject
        {
            ["type"] = "job.changed",
            ["job"] = Job("a", hasKnownTotal: false),
        });

        var job = await seen.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.False(job.HasKnownTotal);
        Assert.Equal(21, job.Done);
        Assert.Equal(0, job.Total);
        Assert.Equal("segments", job.Unit);
    }

    [Fact]
    public async Task A_failure_arrives_classified_rather_than_as_a_sentence()
    {
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        var seen = new TaskCompletionSource<JobSnapshot>(TaskCreationOptions.RunContinuationsAsynchronously);
        client.JobChanged += snapshot => seen.TrySetResult(snapshot);

        var failed = Job("a", state: "failed");
        failed["error"] = new JsonObject
        {
            ["kind"] = "refusal",
            ["code"] = "age_restricted",
            ["message"] = "the provider refused this video",
            ["retryable"] = false,
            ["text"] = "ProviderRefusal: the provider refused this video",
        };
        await core.SendAsync(new JsonObject { ["type"] = "job.changed", ["job"] = failed });

        var job = await seen.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.NotNull(job.Error);
        Assert.Equal("refusal", job.Error!.Kind);
        Assert.False(job.Error.Retryable);
    }

    [Fact]
    public async Task A_removed_job_arrives_by_id_alone()
    {
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        var seen = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
        client.JobRemoved += id => seen.TrySetResult(id);

        await core.SendAsync(new JsonObject { ["type"] = "job.removed", ["jobId"] = "a" });

        Assert.Equal("a", await seen.Task.WaitAsync(TimeSpan.FromSeconds(5)));
    }

    [Fact]
    public async Task Losing_the_core_fails_what_was_waiting_instead_of_hanging()
    {
        // The core does the same in the other direction: when the front end goes,
        // every open ask fails at once rather than waiting out its timeout.
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        var pending = client.ListJobsAsync();
        await core.NextOfTypeAsync("jobs.list");
        core.Disconnect();

        await Assert.ThrowsAsync<CoreDisconnectedException>(() => pending.WaitAsync(TimeSpan.FromSeconds(5)));
    }

    [Fact]
    public async Task An_unreadable_line_costs_that_line_and_nothing_else()
    {
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        var seen = new TaskCompletionSource<JobSnapshot>(TaskCreationOptions.RunContinuationsAsynchronously);
        client.JobChanged += snapshot => seen.TrySetResult(snapshot);

        await core.SendAsync(JsonValue.Create("this is not a message")!);
        await core.SendAsync(new JsonObject { ["type"] = "job.changed", ["job"] = Job("a") });

        // Dropping the bad line keeps every job already on screen alive; closing
        // the connection over it would not.
        var job = await seen.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal("a", job.Id);
    }

    [Fact]
    public async Task The_download_directory_survives_the_round_trip()
    {
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        var pending = client.SetDownloadDirectoryAsync(@"D:\Videos");
        var command = await core.NextOfTypeAsync("settings.setDownloadDirectory");
        Assert.Equal(@"D:\Videos", command.GetProperty("path").GetString());

        await core.ReplyAsync(
            command.GetProperty("id").GetInt64(),
            new JsonObject { ["downloadDirectory"] = @"D:\Videos" });

        Assert.Equal(@"D:\Videos", await pending.WaitAsync(TimeSpan.FromSeconds(5)));
    }

    [Fact]
    public async Task A_directory_that_is_not_set_comes_back_as_nothing()
    {
        await using var core = new FakeCore();
        await using var client = new DownloadCoreClient();
        await client.ConnectAsync(core.Handshake);

        var pending = client.GetDownloadDirectoryAsync();
        var command = await core.NextOfTypeAsync("settings.get");
        await core.ReplyAsync(
            command.GetProperty("id").GetInt64(),
            new JsonObject { ["downloadDirectory"] = null });

        Assert.Null(await pending.WaitAsync(TimeSpan.FromSeconds(5)));
    }
}
