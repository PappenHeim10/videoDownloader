using System.Windows.Input;
using VideoDownloader.Client;

namespace VideoDownloader.Desktop;

/// <summary>What a job entry can ask the window to do with it.</summary>
public interface IJobActions
{
    Task CancelJobAsync(JobViewModel job);

    Task RemoveJobAsync(JobViewModel job);

    Task OpenJobAsync(JobViewModel job);
}

public sealed class JobViewModel : ViewModelBase
{
    /// <summary>MiB - the 1024-based unit, named as such, as the old window did.</summary>
    private const double BytesPerMib = 1024 * 1024;

    private JobSnapshot _snapshot;
    private string? _note;

    public JobViewModel(JobSnapshot snapshot, IJobActions actions)
    {
        _snapshot = snapshot;
        CancelCommand = new RelayCommand(async _ => await actions.CancelJobAsync(this), _ => IsActive);
        RemoveCommand = new RelayCommand(async _ => await actions.RemoveJobAsync(this));
        OpenCommand = new RelayCommand(async _ => await actions.OpenJobAsync(this), _ => CanOpen);
    }

    public string Id => _snapshot.Id;
    public string DisplayTitle => _snapshot.DisplayTitle;
    public string? OutputFile => _snapshot.OutputFile;
    public bool FromDisk => _snapshot.FromDisk;
    public string State => _snapshot.State;

    /// <summary>Every state the core can report, in the user's words. None leaks through raw.</summary>
    public string LocalizedState => _snapshot.State switch
    {
        "created" => "Erstellt",
        "queued" => "Wartet",
        "connecting" => "Verbinde",
        "fetching_metadata" => "Metadaten abrufen",
        "downloading" => "Herunterladen",
        "muxing" => "Spuren werden zusammengefügt",
        "completed" => "Abgeschlossen",
        "failed" => "Fehlgeschlagen",
        "cancelled" => "Abgebrochen",
        _ => _snapshot.State,
    };

    /// <summary>
    /// The counters, in whatever unit the job counts in. A download whose total
    /// is still unknown says how much has arrived and nothing about how much is
    /// left - "0 von 0" would be a claim nobody can make.
    /// </summary>
    public string Details
    {
        get
        {
            if (_snapshot.Unit == "bytes")
            {
                if (_snapshot.Total == 0 && _snapshot.Done == 0)
                {
                    return string.Empty;
                }

                var done = Numbers.OneDecimal(_snapshot.Done / BytesPerMib);
                return _snapshot.Total == 0
                    ? $"{done} MiB"
                    : $"{done} / {Numbers.OneDecimal(_snapshot.Total / BytesPerMib)} MiB";
            }

            return _snapshot.Total > 0 ? $"{_snapshot.Done} / {_snapshot.Total} Segmente" : string.Empty;
        }
    }

    /// <summary>The status line: state, counters and a note if there is one.</summary>
    public string StatusLine
    {
        get
        {
            var parts = new[] { LocalizedState, Details, _note }.Where(part => !string.IsNullOrEmpty(part));
            return string.Join(" · ", parts);
        }
    }

    // A finished download's bar is full, whatever the last counter said (D2).
    public double Progress => _snapshot.State == "completed" ? 100.0 : _snapshot.Progress;

    public bool IsIndeterminate => !_snapshot.HasKnownTotal && IsActive;

    public bool IsActive => _snapshot.State is "created" or "queued" or "connecting"
        or "fetching_metadata" or "downloading" or "muxing";

    /// <summary>Whether there is a finished file to open.</summary>
    public bool CanOpen => !string.IsNullOrEmpty(_snapshot.OutputFile)
        && (_snapshot.State == "completed" || _snapshot.FromDisk);

    /// <summary>Whether removing this entry is a question about a real video file.</summary>
    public bool HasFinishedFile => CanOpen;

    /// <summary>
    /// Why a job failed, on screen rather than in a tooltip (D6), worded by the
    /// kind the core already classified it as.
    /// </summary>
    public string ErrorMessage
    {
        get
        {
            var error = _snapshot.Error;
            if (error is null)
            {
                return string.Empty;
            }

            var prefix = error.Kind switch
            {
                "refusal" => "Abgelehnt",
                "login_required" => "Anmeldung nötig",
                "too_large" => "Nicht gestartet, zu groß",
                "unsupported" => "Nicht unterstützt",
                "selection" => "Keine passende Quelle",
                "track" => "Download fehlgeschlagen",
                "mux" => "Zusammenfügen fehlgeschlagen",
                "filesystem" => "Dateifehler",
                "incomplete" => "Unvollständig",
                _ => "Fehler",
            };
            return string.IsNullOrEmpty(error.Message) ? prefix : $"{prefix}: {error.Message}";
        }
    }

    public bool HasError => _snapshot.Error != null;

    public ICommand CancelCommand { get; }
    public ICommand RemoveCommand { get; }
    public ICommand OpenCommand { get; }

    /// <summary>A short remark that belongs to this entry until its next change, e.g. a missing file.</summary>
    public void ShowNote(string note)
    {
        _note = note;
        OnPropertyChanged(nameof(StatusLine));
    }

    public void Update(JobSnapshot snapshot)
    {
        _snapshot = snapshot;
        _note = null;
        OnPropertyChanged(nameof(DisplayTitle));
        OnPropertyChanged(nameof(State));
        OnPropertyChanged(nameof(LocalizedState));
        OnPropertyChanged(nameof(Details));
        OnPropertyChanged(nameof(StatusLine));
        OnPropertyChanged(nameof(Progress));
        OnPropertyChanged(nameof(IsIndeterminate));
        OnPropertyChanged(nameof(IsActive));
        OnPropertyChanged(nameof(CanOpen));
        OnPropertyChanged(nameof(HasFinishedFile));
        OnPropertyChanged(nameof(OutputFile));
        OnPropertyChanged(nameof(ErrorMessage));
        OnPropertyChanged(nameof(HasError));
        ((RelayCommand)CancelCommand).RaiseCanExecuteChanged();
        ((RelayCommand)OpenCommand).RaiseCanExecuteChanged();
    }
}
