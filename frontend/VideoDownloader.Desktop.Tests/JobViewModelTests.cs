using Xunit;
using VideoDownloader.Client;
using VideoDownloader.Desktop;

namespace VideoDownloader.Desktop.Tests;

public class JobViewModelTests
{
    private JobSnapshot CreateSnapshot(string state = "created", double progress = 0.0, JobFailure? error = null, bool fromDisk = false, bool hasKnownTotal = true)
    {
        return new JobSnapshot(
            Id: "test-id",
            Url: "https://example.com/video",
            Title: "Test Video",
            State: state,
            Progress: progress,
            Done: 0,
            Total: 100,
            Unit: "bytes",
            HasKnownTotal: hasKnownTotal,
            OutputFile: null,
            ExpectedBytes: null,
            Error: error,
            FromDisk: fromDisk,
            Seq: 1
        );
    }

    [Fact]
    public void LocalizedState_ReturnsGermanTranslation()
    {
        // Arrange
        var snapshot = CreateSnapshot(state: "fetching_metadata");
        var vm = new JobViewModel(snapshot, null!);

        // Act
        var state = vm.LocalizedState;

        // Assert
        Assert.Equal("Metadaten abrufen", state);
    }

    [Fact]
    public void Progress_Returns100_WhenCompleted_RegardlessOfSnapshotProgress()
    {
        // Arrange
        var snapshot = CreateSnapshot(state: "completed", progress: 0.5); // Core reported 50%, but it's completed
        var vm = new JobViewModel(snapshot, null!);

        // Act
        var progress = vm.Progress;

        // Assert
        Assert.Equal(100.0, progress);
    }

    [Fact]
    public void ErrorMessage_FormatsCorrectly()
    {
        // Arrange
        var error = new JobFailure("network", "E_NET", "Connection reset", false, "Old text");
        var snapshot = CreateSnapshot(state: "failed", error: error);
        var vm = new JobViewModel(snapshot, null!);

        // Act
        var message = vm.ErrorMessage;

        // Assert
        Assert.True(vm.HasError);
        Assert.Equal("Netzwerkfehler: Connection reset", message);
    }

    [Fact]
    public void IsActive_ReturnsTrueForActiveStates()
    {
        // Arrange
        var vmDownloading = new JobViewModel(CreateSnapshot(state: "downloading"), null!);
        var vmCompleted = new JobViewModel(CreateSnapshot(state: "completed"), null!);

        // Assert
        Assert.True(vmDownloading.IsActive);
        Assert.False(vmCompleted.IsActive);
    }
}
