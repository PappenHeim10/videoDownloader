using VideoDownloader.Client;

namespace VideoDownloader.Desktop;

/// <summary>
/// One login page on screen, and the two things that can end it: the core
/// deciding the login is finished, or the user closing the page.
/// </summary>
/// <remarks>
/// The window only reports what the page holds. Whether that adds up to a
/// session is the core's rule, applied there and tested there - a second copy
/// of it here would sooner or later disagree with the first.
/// </remarks>
public sealed class LoginSession
{
    private readonly Func<IReadOnlyList<SessionCookie>, Task<bool>> _observe;
    private readonly TaskCompletionSource<bool> _finished = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public LoginSession(LoginPage page, bool persists, Func<IReadOnlyList<SessionCookie>, Task<bool>> observe)
    {
        Page = page;
        Persists = persists;
        _observe = observe;
    }

    public LoginPage Page { get; }

    /// <summary>Whether a stored session outlives this run - the page says so before anyone types.</summary>
    public bool Persists { get; }

    /// <summary>Completes when the core ends the login: true signed in, false given up on.</summary>
    public Task<bool> Finished => _finished.Task;

    /// <summary>Report every cookie the page holds right now.</summary>
    public Task<bool> ObserveAsync(IReadOnlyList<SessionCookie> cookies) => _observe(cookies);

    internal void Finish(bool signedIn) => _finished.TrySetResult(signedIn);
}
