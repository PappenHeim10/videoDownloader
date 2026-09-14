using Avalonia;

namespace VideoDownloader.Desktop;

internal static class Program
{
    // The front end is the parent process: it starts the core, and the core ends
    // when this process does. Nothing here may block before the app is built -
    // Avalonia wants the main thread.
    [STAThread]
    public static void Main(string[] args) => BuildAvaloniaApp()
        .StartWithClassicDesktopLifetime(args);

    /// <summary>Used by the Avalonia previewer as well as by <see cref="Main"/>.</summary>
    public static AppBuilder BuildAvaloniaApp() => AppBuilder.Configure<App>()
        .UsePlatformDetect()
        .LogToTrace();
}
