using Avalonia.Controls;
using System.ComponentModel;
using VideoDownloader.Client;

namespace VideoDownloader.Desktop;

public sealed partial class MainWindow : Window, IDialogService
{
    private readonly MainWindowViewModel _viewModel;

    public MainWindow()
    {
        InitializeComponent();
        _viewModel = new MainWindowViewModel(this);
        DataContext = _viewModel;
        
        // Start the core when the window opens
        Opened += async (s, e) => await _viewModel.InitializeAsync();
    }

    protected override async void OnClosing(WindowClosingEventArgs e)
    {
        base.OnClosing(e);
        await _viewModel.DisposeAsync();
    }

    public async Task<bool> ConfirmAsync(string title, string message)
    {
        var dialog = new ConfirmDialog(message) { Title = title };
        return await dialog.ShowDialog<bool>(this);
    }

    public async Task<string?> PickFolderAsync()
    {
        var result = await StorageProvider.OpenFolderPickerAsync(new Avalonia.Platform.Storage.FolderPickerOpenOptions
        {
            Title = "Download-Ordner auswählen",
            AllowMultiple = false
        });
        
        return result.Count > 0 ? result[0].Path.LocalPath : null;
    }
}
