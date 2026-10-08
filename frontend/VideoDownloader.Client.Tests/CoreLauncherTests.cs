using Xunit;

namespace VideoDownloader.Client.Tests;

/// <summary>
/// The front end has to find the same interpreter on every platform it ships to,
/// and a bare "python" would find the system one instead of the project's.
/// </summary>
public sealed class CoreLauncherTests
{
    [Fact]
    public void Finds_the_interpreter_where_Windows_puts_it()
    {
        Assert.Equal(
            Path.Combine(".venv", "Scripts", "python.exe"),
            CoreLauncher.VirtualEnvironmentPython(".venv", windows: true));
    }

    [Fact]
    public void Finds_the_interpreter_where_macOS_and_Linux_put_it()
    {
        Assert.Equal(
            Path.Combine(".venv", "bin", "python"),
            CoreLauncher.VirtualEnvironmentPython(".venv", windows: false));
    }
}
