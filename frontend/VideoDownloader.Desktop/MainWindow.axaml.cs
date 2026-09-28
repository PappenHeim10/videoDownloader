using Avalonia.Controls;
using VideoDownloader.Client;

namespace VideoDownloader.Desktop;

public sealed partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();

        // Reading the constant out of the client assembly is the cheapest proof
        // that the reference is real at runtime and not only at compile time.
        StatusText.Text =
            $"Front end für Host-Protokoll v{CoreHandshake.SupportedProtocol}. "
            + "Der Kern läuft als eigener Prozess und wird über einen Loopback-Socket bedient.";
    }
}
