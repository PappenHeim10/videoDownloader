namespace VideoDownloader.Desktop;

/// <summary>What removing a finished download from the list should do with its file.</summary>
public enum RemoveChoice
{
    Cancel,
    KeepFile,
    DeleteFile,
}

/// <summary>
/// Everything the view model needs a window for. Kept behind an interface so the
/// view model can be tested without one.
/// </summary>
public interface IDialogService
{
    Task<bool> ConfirmAsync(string title, string message, string confirmText = "Bestätigen", string cancelText = "Abbrechen");

    Task<RemoveChoice> AskRemoveAsync(string title, string message);

    Task<string?> PickFolderAsync();

    /// <summary>Show a site's login page until the core finishes the login or the user closes it.</summary>
    /// <returns>True when the core signed the user in, false when the page was closed first.</returns>
    Task<bool> ShowLoginAsync(LoginSession login);

    /// <summary>Open a file with whatever the system opens it with. False when that did not happen.</summary>
    Task<bool> OpenFileAsync(string path);
}
