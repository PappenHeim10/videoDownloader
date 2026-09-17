using System.Collections.ObjectModel;
using System.Windows.Input;
using Avalonia.Threading;
using VideoDownloader.Client;

namespace VideoDownloader.Desktop;

public sealed class MainWindowViewModel : ViewModelBase, IAsyncDisposable
{
    private readonly IDialogService _dialogService;
    private CoreLauncher? _launcher;
    private DownloadCoreClient? _client;
    
    private string _statusText = "Starte Kern...";
    public string StatusText
    {
        get => _statusText;
        set => SetProperty(ref _statusText, value);
    }

    private string _downloadDirectory = "Unbekannt";
    public string DownloadDirectory
    {
        get => _downloadDirectory;
        set => SetProperty(ref _downloadDirectory, value);
    }
    
    private string _newJobUrl = string.Empty;
    public string NewJobUrl
    {
        get => _newJobUrl;
        set => SetProperty(ref _newJobUrl, value);
    }

    public ObservableCollection<JobViewModel> Jobs { get; } = new();
    
    public ICommand AddJobCommand { get; }
    public ICommand ChangeDirectoryCommand { get; }

    public MainWindowViewModel(IDialogService dialogService)
    {
        _dialogService = dialogService;
        AddJobCommand = new RelayCommand(async _ => await AddJobAsync());
        ChangeDirectoryCommand = new RelayCommand(async _ => await ChangeDirectoryAsync());
    }

    public async Task InitializeAsync()
    {
        try
        {
            StatusText = "Starte Kern als Kindprozess...";
            
            // Note: in release this would be the compiled exe. During dev, we use python.
            var command = "python";
            var arguments = "-m video_downloader.host";
            
            var (launcher, handshake) = await CoreLauncher.StartAsync(command, arguments);
            _launcher = launcher;

            StatusText = $"Verbinde auf Port {handshake.Port}...";
            _client = new DownloadCoreClient();
            
            _client.JobCreated += snapshot => Dispatcher.UIThread.Post(() => OnJobCreated(snapshot));
            _client.JobChanged += snapshot => Dispatcher.UIThread.Post(() => OnJobChanged(snapshot));
            _client.JobRemoved += id => Dispatcher.UIThread.Post(() => OnJobRemoved(id));
            _client.Closed += error => Dispatcher.UIThread.Post(() => OnClosed(error));

            var initialJobs = await _client.ConnectAsync(handshake);
            foreach (var job in initialJobs)
            {
                Jobs.Add(new JobViewModel(job, this));
            }
            
            var dir = await _client.GetDownloadDirectoryAsync();
            DownloadDirectory = dir ?? "Nicht konfiguriert";

            StatusText = "Verbunden.";
        }
        catch (Exception ex)
        {
            StatusText = $"Fehler beim Starten des Kerns: {ex.Message}";
        }
    }

    private async Task AddJobAsync()
    {
        if (_client == null || string.IsNullOrWhiteSpace(NewJobUrl)) return;
        
        try
        {
            var url = NewJobUrl;
            NewJobUrl = string.Empty;
            await _client.AddJobAsync(url);
        }
        catch (Exception ex)
        {
            StatusText = $"Fehler beim Hinzufügen: {ex.Message}";
        }
    }

    public async Task CancelJobAsync(JobViewModel jobVm)
    {
        if (_client == null) return;
        try
        {
            await _client.CancelJobAsync(jobVm.Id);
        }
        catch (Exception ex)
        {
            StatusText = $"Fehler beim Abbrechen: {ex.Message}";
        }
    }

    public async Task DeleteJobAsync(JobViewModel jobVm)
    {
        if (_client == null) return;
        try
        {
            bool deleteFile = true;
            if (!jobVm.FromDisk)
            {
                var confirmed = await _dialogService.ConfirmAsync(
                    "Datei löschen?",
                    $"Möchtest du '{jobVm.DisplayTitle}' wirklich von der Festplatte löschen?"
                );
                if (!confirmed) return;
            }
            await _client.DeleteJobAsync(jobVm.Id, deleteFile);
        }
        catch (Exception ex)
        {
            StatusText = $"Fehler beim Löschen: {ex.Message}";
        }
    }

    private async Task ChangeDirectoryAsync()
    {
        if (_client == null) return;
        
        var path = await _dialogService.PickFolderAsync();
        if (path == null) return;
        
        await _client.SetDownloadDirectoryAsync(path);
        DownloadDirectory = path;
        
        // Trigger rescan explicitly
        await _client.RescanAsync();
    }

    private void OnJobCreated(JobSnapshot snapshot)
    {
        Jobs.Add(new JobViewModel(snapshot, this));
    }

    private void OnJobChanged(JobSnapshot snapshot)
    {
        var existing = Jobs.FirstOrDefault(j => j.Id == snapshot.Id);
        if (existing != null)
        {
            existing.Update(snapshot);
        }
    }

    private void OnJobRemoved(string id)
    {
        var existing = Jobs.FirstOrDefault(j => j.Id == id);
        if (existing != null)
        {
            Jobs.Remove(existing);
        }
    }

    private void OnClosed(Exception? error)
    {
        StatusText = error == null ? "Kern regulär beendet." : $"Verbindung getrennt: {error.Message}";
    }

    public async ValueTask DisposeAsync()
    {
        if (_client != null)
        {
            try 
            { 
                await _client.ShutdownAsync(); 
            } 
            catch (Exception) 
            { 
                // Ignore shutdown errors, we are disposing anyway
            }
            await _client.DisposeAsync();
        }
        _launcher?.Dispose();
    }
}
