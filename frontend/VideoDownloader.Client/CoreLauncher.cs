using System.Diagnostics;

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
            UseShellExecute = false,
            RedirectStandardOutput = true,
            CreateNoWindow = true
        };

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
