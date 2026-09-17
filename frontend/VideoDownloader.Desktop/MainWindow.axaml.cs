using Avalonia.Controls;
using System.ComponentModel;
using VideoDownloader.Client;

namespace VideoDownloader.Desktop;

public sealed partial class MainWindow : Window
{
    private MainWindowViewModel? _viewModel;

    public MainWindow()
    {
        InitializeComponent();
        _viewModel = new MainWindowViewModel();
        DataContext = _viewModel;
        
        // Start the core when the window opens
        Opened += async (s, e) => await _viewModel.InitializeAsync();
    }

    protected override async void OnClosing(WindowClosingEventArgs e)
    {
        base.OnClosing(e);
        if (_viewModel != null)
        {
            await _viewModel.DisposeAsync();
        }
    }
}
