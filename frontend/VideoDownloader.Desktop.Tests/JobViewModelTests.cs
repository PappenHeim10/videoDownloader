using Xunit;
using VideoDownloader.Client;
using VideoDownloader.Desktop;

namespace VideoDownloader.Desktop.Tests;

public class JobViewModelTests
{
    private static JobSnapshot CreateSnapshot(
        string state = "created",
        double progress = 0.0,
        JobFailure? error = null,
        bool fromDisk = false,
        bool hasKnownTotal = true,
        long done = 0,
        long total = 100,
        string unit = "bytes",
        string? outputFile = null)
    {
        return new JobSnapshot(
            Id: "test-id",
            Url: "https://example.com/video",
            Title: "Test Video",
            State: state,
            Progress: progress,
            Done: done,
            Total: total,
            Unit: unit,
            HasKnownTotal: hasKnownTotal,
            OutputFile: outputFile,
            ExpectedBytes: null,
            Error: error,
            FromDisk: fromDisk,
            Seq: 1
        );
    }

    [Fact]
    public void LocalizedState_ReturnsGermanTranslation()
    {
        var vm = new JobViewModel(CreateSnapshot(state: "fetching_metadata"), null!);

        Assert.Equal("Metadaten abrufen", vm.LocalizedState);
    }

    [Theory]
    [InlineData("created")]
    [InlineData("queued")]
    [InlineData("connecting")]
    [InlineData("fetching_metadata")]
    [InlineData("downloading")]
    [InlineData("muxing")]
    [InlineData("completed")]
    [InlineData("cancelled")]
    [InlineData("failed")]
    public void No_state_the_core_reports_reaches_the_window_raw(string state)
    {
        // The old window showed "fetching_metadata" in a German sentence (§4.1).
        var vm = new JobViewModel(CreateSnapshot(state: state), null!);

        Assert.NotEqual(state, vm.LocalizedState);
    }

    [Fact]
    public void Progress_Returns100_WhenCompleted_RegardlessOfSnapshotProgress()
    {
        var vm = new JobViewModel(CreateSnapshot(state: "completed", progress: 0.5), null!);

        Assert.Equal(100.0, vm.Progress);
    }

    [Fact]
    public void A_failure_is_worded_by_the_kind_the_core_gave_it()
    {
        var error = new JobFailure("login_required", "x.login", "Dieser Beitrag braucht eine Anmeldung", false, "old text");
        var vm = new JobViewModel(CreateSnapshot(state: "failed", error: error), null!);

        Assert.True(vm.HasError);
        Assert.Equal("Anmeldung nötig: Dieser Beitrag braucht eine Anmeldung", vm.ErrorMessage);
    }

    [Fact]
    public void An_unknown_kind_still_reads_as_an_error()
    {
        var error = new JobFailure("something_new", "c", "Etwas ging schief", false, "t");
        var vm = new JobViewModel(CreateSnapshot(state: "failed", error: error), null!);

        Assert.Equal("Fehler: Etwas ging schief", vm.ErrorMessage);
    }

    [Fact]
    public void IsActive_ReturnsTrueForActiveStates()
    {
        Assert.True(new JobViewModel(CreateSnapshot(state: "downloading"), null!).IsActive);
        Assert.True(new JobViewModel(CreateSnapshot(state: "queued"), null!).IsActive);
        Assert.False(new JobViewModel(CreateSnapshot(state: "completed"), null!).IsActive);
    }

    [Fact]
    public void Bytes_are_counted_in_MiB()
    {
        var vm = new JobViewModel(
            CreateSnapshot(state: "downloading", done: 5 * 1024 * 1024, total: 20 * 1024 * 1024), null!);

        Assert.Equal("5,0 / 20,0 MiB", vm.Details);
    }

    [Fact]
    public void An_unknown_total_says_only_what_has_arrived()
    {
        var vm = new JobViewModel(
            CreateSnapshot(state: "downloading", done: 3 * 1024 * 1024, total: 0, hasKnownTotal: false), null!);

        Assert.Equal("3,0 MiB", vm.Details);
        Assert.True(vm.IsIndeterminate);
    }

    [Fact]
    public void Segments_are_counted_as_segments()
    {
        var vm = new JobViewModel(CreateSnapshot(state: "downloading", done: 12, total: 56, unit: "segments"), null!);

        Assert.Equal("12 / 56 Segmente", vm.Details);
    }

    [Fact]
    public void Only_a_finished_file_can_be_opened()
    {
        var running = new JobViewModel(CreateSnapshot(state: "downloading", outputFile: "a.mp4"), null!);
        var finished = new JobViewModel(CreateSnapshot(state: "completed", outputFile: "a.mp4"), null!);
        var onDisk = new JobViewModel(CreateSnapshot(state: "completed", fromDisk: true, outputFile: "b.mp4"), null!);
        var noFile = new JobViewModel(CreateSnapshot(state: "completed"), null!);

        Assert.False(running.CanOpen);
        Assert.True(finished.CanOpen);
        Assert.True(onDisk.CanOpen);
        Assert.False(noFile.CanOpen);
    }

    [Fact]
    public void A_note_stays_until_the_next_change()
    {
        var vm = new JobViewModel(CreateSnapshot(state: "completed", outputFile: "gone.mp4"), null!);

        vm.ShowNote("Datei nicht gefunden");
        Assert.Contains("Datei nicht gefunden", vm.StatusLine);

        vm.Update(CreateSnapshot(state: "completed", outputFile: "gone.mp4"));
        Assert.DoesNotContain("Datei nicht gefunden", vm.StatusLine);
    }
}
