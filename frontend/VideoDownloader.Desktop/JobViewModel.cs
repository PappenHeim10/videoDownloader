using System.Windows.Input;
using VideoDownloader.Client;

namespace VideoDownloader.Desktop;

public sealed class JobViewModel : ViewModelBase
{
    private JobSnapshot _snapshot;
    private readonly MainWindowViewModel _parent;

    public JobViewModel(JobSnapshot snapshot, MainWindowViewModel parent)
    {
        _snapshot = snapshot;
        _parent = parent;
        CancelCommand = new RelayCommand(async _ => await _parent.CancelJobAsync(this), _ => IsActive);
        DeleteCommand = new RelayCommand(async _ => await _parent.DeleteJobAsync(this));
    }

    public string Id => _snapshot.Id;
    public string DisplayTitle => _snapshot.DisplayTitle;
    
    public string LocalizedState
    {
        get
        {
            return _snapshot.State switch
            {
                "created" => "Erstellt",
                "fetching_metadata" => "Metadaten abrufen",
                "downloading" => "Herunterladen",
                "muxing" => "Zusammenfügen",
                "completed" => "Abgeschlossen",
                "failed" => "Fehlgeschlagen",
                "cancelled" => "Abgebrochen",
                _ => _snapshot.State
            };
        }
    }
    
    // Feature: Progress Bar completes to 100% on COMPLETED
    public double Progress => _snapshot.State == "completed" ? 100.0 : _snapshot.Progress;
    
    public bool IsIndeterminate => !_snapshot.HasKnownTotal && _snapshot.State != "completed";
    
    public bool IsActive => _snapshot.State is "created" or "fetching_metadata" or "downloading" or "muxing";
    
    public bool FromDisk => _snapshot.FromDisk;

    // Feature: Show structured errors
    public string ErrorMessage
    {
        get
        {
            if (_snapshot.Error == null) return string.Empty;
            return _snapshot.Error.Kind switch
            {
                "refusal" => $"Ablehnung: {_snapshot.Error.Message}",
                "network" => $"Netzwerkfehler: {_snapshot.Error.Message}",
                "cancelled" => "Download durch Benutzer abgebrochen",
                _ => $"Fehler: {_snapshot.Error.Message}"
            };
        }
    }
    
    public bool HasError => _snapshot.Error != null;

    public ICommand CancelCommand { get; }
    public ICommand DeleteCommand { get; }

    public void Update(JobSnapshot snapshot)
    {
        _snapshot = snapshot;
        OnPropertyChanged(nameof(DisplayTitle));
        OnPropertyChanged(nameof(LocalizedState));
        OnPropertyChanged(nameof(Progress));
        OnPropertyChanged(nameof(IsIndeterminate));
        OnPropertyChanged(nameof(IsActive));
        OnPropertyChanged(nameof(ErrorMessage));
        OnPropertyChanged(nameof(HasError));
        ((RelayCommand)CancelCommand).RaiseCanExecuteChanged();
    }
}
