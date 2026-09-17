namespace VideoDownloader.Desktop;

public interface IDialogService
{
    Task<bool> ConfirmAsync(string title, string message);
    Task<string?> PickFolderAsync();
}
