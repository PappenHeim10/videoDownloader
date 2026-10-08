using System.Diagnostics;
using System.Text;

namespace VideoDownloader.Client;

/// <summary>
/// Starts the video downloader core as a child process and reads its handshake.
/// </summary>
public sealed class CoreLauncher : IDisposable
{
    private readonly Process _process;
    private bool _disposed;

    private CoreLauncher(Process process)
    {
        _process = process;
    }

    /// <summary>
    /// The interpreter inside a virtual environment on the platform this runs on.
    /// </summary>
    public static string VirtualEnvironmentPython(string virtualEnvironment) =>
        VirtualEnvironmentPython(virtualEnvironment, OperatingSystem.IsWindows());

    /// <summary>
    /// The interpreter inside a virtual environment. Windows puts it under
    /// <c>Scripts\python.exe</c>, every other platform under <c>bin/python</c>;
    /// a front end that assumes one layout cannot start the core on the other.
    /// </summary>
    public static string VirtualEnvironmentPython(string virtualEnvironment, bool windows) =>
        windows
            ? Path.Combine(virtualEnvironment, "Scripts", "python.exe")
            : Path.Combine(virtualEnvironment, "bin", "python");

    /// <summary>
    /// Starts the core process. The caller is responsible for disposing the launcher
    /// when the application exits, which will terminate the child process.
    /// </summary>
    public static async Task<(CoreLauncher Launcher, CoreHandshake Handshake)> StartAsync(
        string command, string arguments, CancellationToken cancellationToken = default)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = command,
            Arguments = arguments,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardOutputEncoding = Encoding.UTF8,
        };

        // Ensure Python can find the 'video_downloader' package inside the 'src' directory
        startInfo.EnvironmentVariables["PYTHONPATH"] = "src";

        var process = new Process { StartInfo = startInfo };
        if (!process.Start())
        {
            throw new CoreProtocolException($"Failed to start core process: {command} {arguments}");
        }

        // Read the very first line from stdout which must be the handshake.
        var readTask = process.StandardOutput.ReadLineAsync();
        var line = await readTask.WaitAsync(cancellationToken).ConfigureAwait(false);
        
        if (line is null)
        {
            process.Kill();
            throw new CoreProtocolException("Core process ended without sending a handshake");
        }

        var handshake = CoreHandshake.Parse(line);
        return (new CoreLauncher(process), handshake);
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;

        if (!_process.HasExited)
        {
            try
            {
                _process.Kill();
            }
            catch (Exception)
            {
                // Process may have exited right before Kill()
            }
        }
        
        _process.Dispose();
    }
}
