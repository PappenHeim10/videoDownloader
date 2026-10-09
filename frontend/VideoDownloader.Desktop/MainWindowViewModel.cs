using System.Collections.Concurrent;
using System.Collections.ObjectModel;
using System.Windows.Input;
using Avalonia.Threading;
using VideoDownloader.Client;

namespace VideoDownloader.Desktop;

/// <summary>
/// The window's state and the nine things a user can do in it (migration plan
/// §3.3). Every rule stays in the core; what is here is wording, asking, and
/// marshalling onto the UI thread.
/// </summary>
public sealed class MainWindowViewModel : ViewModelBase, IJobActions, IAsyncDisposable
{
    private const double BytesPerGib = 1024.0 * 1024 * 1024;

    /// <summary>How long the core gets to wind its downloads down before it is ended.</summary>
    /// <remarks>The core's own stop gives each download 10 s of grace before it escalates.</remarks>
    private static readonly TimeSpan CoreExitGrace = TimeSpan.FromSeconds(15);

    private readonly IDialogService _dialogs;
    private readonly ConcurrentDictionary<string, LoginSession> _logins = new();
    private CoreLauncher? _launcher;
    private DownloadCoreClient? _client;
    private bool _sessionsPersist;

    private string _statusText = "Starte Kern...";
    public string StatusText
    {
        get => _statusText;
        set => SetProperty(ref _statusText, value);
    }

    private string? _downloadDirectory;
    public string? DownloadDirectory
    {
        get => _downloadDirectory;
        private set
        {
            if (SetProperty(ref _downloadDirectory, value))
            {
                OnPropertyChanged(nameof(DownloadDirectoryText));
            }
        }
    }

    public string DownloadDirectoryText => DownloadDirectory ?? "Noch keiner gewählt";

    private string _newJobUrl = string.Empty;
    public string NewJobUrl
    {
        get => _newJobUrl;
        set => SetProperty(ref _newJobUrl, value);
    }

    private string _quality = "best";
    public string Quality
    {
        get => _quality;
        set => SetProperty(ref _quality, value);
    }

    private bool _isConnected;
    public bool IsConnected
    {
        get => _isConnected;
        private set => SetProperty(ref _isConnected, value);
    }

    public ObservableCollection<JobViewModel> Jobs { get; } = new();

    /// <summary>The sites a login can be started for, as the core names them.</summary>
    public ObservableCollection<SiteLoginViewModel> SiteLogins { get; } = new();

    public ICommand AddJobCommand { get; }
    public ICommand ChangeDirectoryCommand { get; }
    public ICommand RescanCommand { get; }

    public MainWindowViewModel(IDialogService dialogs)
    {
        _dialogs = dialogs;
        AddJobCommand = new RelayCommand(async _ => await AddJobAsync());
        ChangeDirectoryCommand = new RelayCommand(async _ => await ChangeDirectoryAsync());
        RescanCommand = new RelayCommand(async _ => await RescanAsync());
    }

    public async Task InitializeAsync()
    {
        try
        {
            StatusText = "Starte Kern als Kindprozess...";

            // Note: in release this would be the compiled exe. During dev, we use the
            // repository's virtual environment, never whatever "python" is on PATH.
            var command = CoreLauncher.VirtualEnvironmentPython(".venv");
            var arguments = "-m video_downloader.host";

            var (launcher, handshake) = await CoreLauncher.StartAsync(command, arguments);
            _launcher = launcher;

            StatusText = $"Verbinde auf Port {handshake.Port}...";
            _client = new DownloadCoreClient
            {
                ConfirmLargeDownload = ConfirmLargeDownloadAsync,
                RequestLogin = (ask, _) => RunLoginAsync(ask.Page),
            };

            // Events arrive on the client's reader thread. The window is not
            // touched from there - everything goes through the dispatcher.
            _client.JobCreated += snapshot => Dispatcher.UIThread.Post(() => OnJobCreated(snapshot));
            _client.JobChanged += snapshot => Dispatcher.UIThread.Post(() => OnJobChanged(snapshot));
            _client.JobRemoved += id => Dispatcher.UIThread.Post(() => OnJobRemoved(id));
            _client.LoginFinished += outcome => Dispatcher.UIThread.Post(() => OnLoginFinished(outcome));
            _client.Closed += error => Dispatcher.UIThread.Post(() => OnClosed(error));

            ReplaceJobs(await _client.ConnectAsync(handshake));
            DownloadDirectory = await _client.GetDownloadDirectoryAsync();
            await RefreshSessionsAsync();

            IsConnected = true;
            StatusText = "Verbunden.";
        }
        catch (Exception ex)
        {
            StatusText = $"Fehler beim Starten des Kerns: {ex.Message}";
        }
    }

    // --- 1. Download starten -------------------------------------------------

    private async Task AddJobAsync()
    {
        if (_client is null)
        {
            return;
        }

        var url = NewJobUrl.Trim();
        if (url.Length == 0)
        {
            return;
        }

        try
        {
            // The first download asks where downloads go, as the old window did.
            if (DownloadDirectory is null)
            {
                var chosen = await _dialogs.PickFolderAsync();
                if (chosen is null)
                {
                    // Cancelling the picker is a decision, not an error.
                    StatusText = "Download abgebrochen: kein Zielordner gewählt.";
                    return;
                }

                DownloadDirectory = await _client.SetDownloadDirectoryAsync(chosen);
            }

            NewJobUrl = string.Empty;
            await _client.AddJobAsync(url, string.IsNullOrWhiteSpace(Quality) ? "best" : Quality.Trim());
        }
        catch (Exception ex)
        {
            StatusText = $"Fehler beim Hinzufügen: {ex.Message}";
        }
    }

    // --- 2. Abbrechen und Entfernen, getrennt (M6) --------------------------

    public async Task CancelJobAsync(JobViewModel job)
    {
        if (_client is null)
        {
            return;
        }

        try
        {
            await _client.CancelJobAsync(job.Id);
        }
        catch (Exception ex)
        {
            StatusText = $"Fehler beim Abbrechen: {ex.Message}";
        }
    }

    public async Task RemoveJobAsync(JobViewModel job)
    {
        if (_client is null)
        {
            return;
        }

        try
        {
            // An unfinished download leaves only partial data behind, and taking
            // it away with the entry is what the old "X" always did. A finished
            // file is a real video - for an entry found on disk, one nobody
            // downloaded in this session - so that is asked, never implied (D4).
            var deleteFile = true;
            if (job.HasFinishedFile)
            {
                var choice = await _dialogs.AskRemoveAsync(
                    "Eintrag entfernen",
                    $"„{job.DisplayTitle}“ aus der Liste entfernen?\n\nDie Videodatei kann erhalten bleiben oder mitgelöscht werden.");
                if (choice == RemoveChoice.Cancel)
                {
                    return;
                }

                deleteFile = choice == RemoveChoice.DeleteFile;
            }

            await _client.DeleteJobAsync(job.Id, deleteFile);
        }
        catch (Exception ex)
        {
            StatusText = $"Fehler beim Entfernen: {ex.Message}";
        }
    }

    // --- 3. Fertige Datei öffnen --------------------------------------------

    public async Task OpenJobAsync(JobViewModel job)
    {
        var path = job.OutputFile;
        if (string.IsNullOrEmpty(path) || !File.Exists(path))
        {
            job.ShowNote("Datei nicht gefunden");
            return;
        }

        if (!await _dialogs.OpenFileAsync(path))
        {
            job.ShowNote("Datei konnte nicht geöffnet werden");
        }
    }

    // --- 4. Download-Ordner ändern, und neu einlesen -------------------------

    private async Task ChangeDirectoryAsync()
    {
        if (_client is null)
        {
            return;
        }

        try
        {
            var path = await _dialogs.PickFolderAsync();
            if (path is null)
            {
                return;
            }

            // The core rescans on its own when the folder changes; the list is
            // rebuilt from that, or it would go on describing the old folder (D3).
            DownloadDirectory = await _client.SetDownloadDirectoryAsync(path);
            ReplaceJobs(await _client.RescanAsync());
            StatusText = $"Download-Ordner: {DownloadDirectory}. Gilt für neue Downloads.";
        }
        catch (Exception ex)
        {
            StatusText = $"Ordner konnte nicht gesetzt werden: {ex.Message}";
        }
    }

    private async Task RescanAsync()
    {
        if (_client is null)
        {
            return;
        }

        try
        {
            ReplaceJobs(await _client.RescanAsync());
            StatusText = "Download-Ordner neu eingelesen.";
        }
        catch (Exception ex)
        {
            StatusText = $"Einlesen fehlgeschlagen: {ex.Message}";
        }
    }

    // --- 5./6./8. An- und Abmelden ------------------------------------------

    internal async Task SignInAsync(string site)
    {
        if (_client is null)
        {
            return;
        }

        try
        {
            var page = await _client.StartLoginAsync(site);
            await RunLoginAsync(page);
        }
        catch (Exception ex)
        {
            StatusText = $"Anmeldung bei {site} nicht möglich: {ex.Message}";
        }
    }

    internal async Task SignOutAsync(string site)
    {
        if (_client is null)
        {
            return;
        }

        try
        {
            var removed = await _client.ClearSessionAsync(site);
            StatusText = removed
                ? $"Von {site} abgemeldet."
                : $"Es war keine Anmeldung für {site} gespeichert.";
            await RefreshSessionsAsync();
        }
        catch (Exception ex)
        {
            StatusText = $"Abmelden fehlgeschlagen: {ex.Message}";
        }
    }

    /// <summary>
    /// Show a login page until the core finishes the login or the user closes
    /// it. The same path whether the user asked for the login or a job did.
    /// </summary>
    private async Task<bool> RunLoginAsync(LoginPage page)
    {
        var client = _client;
        if (client is null)
        {
            return false;
        }

        var login = new LoginSession(
            page, _sessionsPersist, cookies => client.ObserveLoginAsync(page.LoginId, cookies));
        _logins[page.LoginId] = login;
        try
        {
            var signedIn = await Dispatcher.UIThread.InvokeAsync(() => _dialogs.ShowLoginAsync(login));
            if (!signedIn)
            {
                try
                {
                    await client.CancelLoginAsync(page.LoginId);
                }
                catch (Exception)
                {
                    // The core ends every open login when the connection goes;
                    // there is nothing left to cancel then.
                }

                Dispatcher.UIThread.Post(() => StatusText = $"Anmeldung bei {page.Site} abgebrochen.");
            }

            return signedIn;
        }
        finally
        {
            _logins.TryRemove(page.LoginId, out _);
        }
    }

    private void OnLoginFinished(LoginOutcome outcome)
    {
        if (_logins.TryGetValue(outcome.LoginId, out var login))
        {
            login.Finish(outcome.SignedIn);
        }

        if (outcome.SignedIn)
        {
            StatusText = outcome.Persisted
                ? $"Bei {outcome.Site} angemeldet."
                : $"Bei {outcome.Site} angemeldet - gilt nur für diese Sitzung.";
            _ = RefreshSessionsAsync();
        }
    }

    private async Task RefreshSessionsAsync()
    {
        if (_client is null)
        {
            return;
        }

        try
        {
            var overview = await _client.ListSessionsAsync();
            _sessionsPersist = overview.Persists;
            SiteLogins.Clear();
            foreach (var login in overview.Logins)
            {
                SiteLogins.Add(new SiteLoginViewModel(login.Site, overview.Sites.Contains(login.Site), this));
            }
        }
        catch (Exception)
        {
            // The list of logins is a convenience; a failure to read it leaves
            // the previous one on screen rather than taking the window down.
        }
    }

    // --- 7. Großen Download bestätigen --------------------------------------

    private async Task<bool> ConfirmLargeDownloadAsync(LargeDownloadAsk ask, CancellationToken cancellationToken)
    {
        // An estimate, so the wording says "about" rather than pretending to a
        // precision it does not have.
        var gib = Numbers.OneDecimal((ask.EstimatedBytes ?? 0) / BytesPerGib);
        var title = ask.Title ?? "Dieser Download";
        var confirmed = await Dispatcher.UIThread.InvokeAsync(() => _dialogs.ConfirmAsync(
            "Großer Download",
            $"„{title}“ ist etwa {gib} GiB groß.\n\nFortfahren?",
            confirmText: "Ja",
            cancelText: "Nein"));

        if (!confirmed)
        {
            Dispatcher.UIThread.Post(() => StatusText = $"Download abgebrochen: {gib} GiB waren zu viel.");
        }

        return confirmed;
    }

    // --- events -------------------------------------------------------------

    private void ReplaceJobs(IEnumerable<JobSnapshot> jobs)
    {
        Jobs.Clear();
        foreach (var job in jobs)
        {
            Jobs.Add(new JobViewModel(job, this));
        }
    }

    private void OnJobCreated(JobSnapshot snapshot)
    {
        // The result of jobs.add and this event can race; one entry per job.
        if (Jobs.Any(job => job.Id == snapshot.Id))
        {
            OnJobChanged(snapshot);
            return;
        }

        Jobs.Add(new JobViewModel(snapshot, this));
    }

    private void OnJobChanged(JobSnapshot snapshot)
    {
        Jobs.FirstOrDefault(job => job.Id == snapshot.Id)?.Update(snapshot);
    }

    private void OnJobRemoved(string id)
    {
        var existing = Jobs.FirstOrDefault(job => job.Id == id);
        if (existing is not null)
        {
            Jobs.Remove(existing);
        }
    }

    private void OnClosed(Exception? error)
    {
        IsConnected = false;
        foreach (var login in _logins.Values)
        {
            login.Finish(false);
        }

        if (!_shuttingDown)
        {
            StatusText = error is null
                ? "Der Kern hat die Verbindung beendet."
                : $"Verbindung zum Kern verloren: {error.Message}";
        }
    }

    // --- 9. Anwendung schließen ---------------------------------------------

    private bool _shuttingDown;

    /// <summary>
    /// Ask the core to wind down - it stops every download with a deadline and
    /// keeps their resume state - then wait for it to exit, and end it only if
    /// it does not.
    /// </summary>
    public async ValueTask DisposeAsync()
    {
        if (_shuttingDown)
        {
            return;
        }

        _shuttingDown = true;
        StatusText = "Beende...";

        if (_client is not null)
        {
            try
            {
                using var deadline = new CancellationTokenSource(CoreExitGrace);
                await _client.ShutdownAsync(deadline.Token);
            }
            catch (Exception)
            {
                // A core that cannot be asked is ended below instead.
            }

            await _client.DisposeAsync();
        }

        if (_launcher is not null)
        {
            await _launcher.WaitForExitAsync(CoreExitGrace);
            _launcher.Dispose();
        }
    }
}

/// <summary>One site a login can be started for, and whether a session for it is stored.</summary>
public sealed class SiteLoginViewModel : ViewModelBase
{
    public SiteLoginViewModel(string site, bool isSignedIn, MainWindowViewModel window)
    {
        Site = site;
        IsSignedIn = isSignedIn;
        SignInCommand = new RelayCommand(async _ => await window.SignInAsync(site));
        SignOutCommand = new RelayCommand(async _ => await window.SignOutAsync(site));
    }

    public string Site { get; }

    public bool IsSignedIn { get; }

    public string Label => IsSignedIn ? $"{Site}: angemeldet" : $"{Site}: nicht angemeldet";

    public ICommand SignInCommand { get; }

    public ICommand SignOutCommand { get; }
}
