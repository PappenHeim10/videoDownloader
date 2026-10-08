using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Platform.Storage;

namespace VideoDownloader.Desktop;

public sealed partial class MainWindow : Window, IDialogService
{
    private readonly MainWindowViewModel _viewModel;
    private bool _closing;
    private bool _shutDown;

    public MainWindow()
    {
        InitializeComponent();
        _viewModel = new MainWindowViewModel(this);
        DataContext = _viewModel;

        // Start the core when the window opens
        Opened += async (_, _) => await _viewModel.InitializeAsync();
    }

    /// <summary>
    /// Closing is two steps. The first close is refused and starts the shutdown -
    /// the core stops its downloads with a deadline and keeps their resume
    /// state - and the window closes itself once that is done. Closing while
    /// that runs is refused again rather than starting a second shutdown.
    /// </summary>
    protected override void OnClosing(WindowClosingEventArgs e)
    {
        base.OnClosing(e);
        if (_shutDown)
        {
            return;
        }

        e.Cancel = true;
        if (_closing)
        {
            return;
        }

        _closing = true;
        _ = ShutDownAndCloseAsync();
    }

    private async Task ShutDownAndCloseAsync()
    {
        IsEnabled = false;
        try
        {
            await _viewModel.DisposeAsync();
        }
        finally
        {
            _shutDown = true;
            Close();
        }
    }

    private void OnJobDoubleTapped(object? sender, TappedEventArgs e)
    {
        if (sender is Control { DataContext: JobViewModel job } && job.OpenCommand.CanExecute(null))
        {
            job.OpenCommand.Execute(null);
        }
    }

    // --- IDialogService -----------------------------------------------------

    public async Task<bool> ConfirmAsync(string title, string message, string confirmText = "Bestätigen", string cancelText = "Abbrechen")
    {
        // The cautious answer is on the left and is the default, as the old
        // window's "No" was.
        var dialog = new ConfirmDialog(message, [cancelText, confirmText], defaultAnswer: 0) { Title = title };
        return await dialog.ShowDialog<int>(this) == 1;
    }

    public async Task<RemoveChoice> AskRemoveAsync(string title, string message)
    {
        var dialog = new ConfirmDialog(
            message, ["Abbrechen", "Datei behalten", "Datei löschen"], defaultAnswer: 0) { Title = title };
        return await dialog.ShowDialog<int>(this) switch
        {
            1 => RemoveChoice.KeepFile,
            2 => RemoveChoice.DeleteFile,
            _ => RemoveChoice.Cancel,
        };
    }

    public async Task<string?> PickFolderAsync()
    {
        var result = await StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions
        {
            Title = "Download-Ordner auswählen",
            AllowMultiple = false,
        });

        return result.Count > 0 ? result[0].Path.LocalPath : null;
    }

    public Task<bool> ShowLoginAsync(LoginSession login)
    {
        // Shown, not ShowDialog-ed: a login takes as long as somebody takes to
        // type, and the downloads in this window keep running meanwhile.
        var window = new LoginWindow(login);
        window.Show(this);
        return window.Result;
    }

    public async Task<bool> OpenFileAsync(string path)
    {
        var launcher = GetTopLevel(this)?.Launcher;
        return launcher is not null && await launcher.LaunchFileInfoAsync(new FileInfo(path));
    }
}
