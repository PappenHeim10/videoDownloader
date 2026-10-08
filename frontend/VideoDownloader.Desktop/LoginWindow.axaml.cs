using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.Platform;
using Avalonia.Threading;
using VideoDownloader.Client;

namespace VideoDownloader.Desktop;

/// <summary>
/// The site's own login page, kept for nothing but its cookies.
/// </summary>
/// <remarks>
/// <para>
/// Why the site's page and not a form of our own: X's login is a captcha, a
/// password, sometimes a two-factor code and sometimes an e-mail challenge. A
/// form here would fail at the first challenge, and until it failed it would be
/// holding a password that is not ours to hold.
/// </para>
/// <para>
/// The page is watched, not listened to: the WebView has no cookie events on
/// any platform, so every 250 ms the window reports every cookie the page holds
/// and the core decides when they add up to a login. Against the core's 2 s
/// settle period that is eight observations - measured at 1.74 ms each on
/// Windows (vault §20.3).
/// </para>
/// <para>
/// Off the record on every platform that offers it: nothing the page sets is
/// written to disk beside the application's own session store. On Windows that
/// is verified (§20.4); on macOS and Linux the switches exist by name and are
/// not yet measured (issues #12, #13).
/// </para>
/// </remarks>
public sealed partial class LoginWindow : Window
{
    private static readonly TimeSpan PollInterval = TimeSpan.FromMilliseconds(250);

    private readonly LoginSession? _login;
    private readonly DispatcherTimer _poll;
    private readonly TaskCompletionSource<bool> _result = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private bool _observing;
    private bool _signedIn;

    public LoginWindow()
    {
        InitializeComponent();
        _poll = new DispatcherTimer { Interval = PollInterval };
    }

    public LoginWindow(LoginSession login) : this()
    {
        _login = login;
        var page = login.Page;

        Title = $"Bei {page.Site} anmelden";
        Instruction.Text =
            $"Melde dich auf der Seite von {page.Site} an. Das Fenster schließt sich, sobald die Anmeldung erkannt wird.";
        Note.Text =
            $"Gespeichert werden nur die Sitzungscookies ({string.Join(", ", page.RequiredCookies)})"
            + (login.Persists
                ? " - verschlüsselt für dieses Benutzerkonto."
                : " - und nur für diese Sitzung, weil auf diesem System kein sicherer Speicher bereitsteht.")
            + " Kein Passwort erreicht diese Anwendung.";

        Page.EnvironmentRequested += OnEnvironmentRequested;
        Page.Source = new Uri(page.LoginUrl);

        _poll.Tick += (_, _) => _ = PollAsync();
        _poll.Start();

        login.Finished.ContinueWith(
            finished => Dispatcher.UIThread.Post(() =>
            {
                _signedIn = finished.Result;
                Close();
            }),
            TaskScheduler.Default);
    }

    /// <summary>Completes when the window closes: true if the core signed the user in.</summary>
    public Task<bool> Result => _result.Task;

    private static void OnEnvironmentRequested(object? sender, WebViewEnvironmentRequestedEventArgs e)
    {
        switch (e)
        {
            case WindowsWebView2EnvironmentRequestedEventArgs windows:
                // Measured on 2026-09-14: with this, 0 cookie rows, 0 history
                // entries and an empty cache; without it, 7 cookies and 3 URLs.
                windows.IsInPrivateModeEnabled = true;
                break;

            case AppleWKWebViewEnvironmentRequestedEventArgs apple:
                apple.NonPersistentDataStore = true;
                break;

            case GtkWebViewEnvironmentRequestedEventArgs gtk:
                gtk.EphemeralDataManager = true;
                break;

            case LinuxWpeWebViewEnvironmentRequestedEventArgs wpe:
                // WPE has no ephemeral mode at all - only data and cache
                // directories. A login page that writes its cookies to disk is
                // the one thing this window must not do, so WebKitGTK is asked
                // for instead. Whether WPE should be the default anyway is
                // issue #13; until it is decided, privacy wins.
                wpe.PreferWebKitGtkInstead = true;
                break;
        }
    }

    private async Task PollAsync()
    {
        // One snapshot at a time. A slow round trip must not pile up a queue of
        // stale snapshots behind it.
        if (_observing || _login is null)
        {
            return;
        }

        _observing = true;
        try
        {
            // Null for the first few polls, until the platform adapter stands.
            var manager = Page.TryGetCookieManager();
            if (manager is null)
            {
                return;
            }

            var cookies = await manager.GetCookiesAsync();
            var snapshot = cookies
                .Select(cookie => new SessionCookie(cookie.Name, cookie.Value, cookie.Domain))
                .ToList();
            await _login.ObserveAsync(snapshot);
        }
        catch (Exception)
        {
            // A failed poll is one missed observation out of eight inside the
            // settle period; the next tick tries again. Ending the login over
            // it would throw away whatever the user has typed.
        }
        finally
        {
            _observing = false;
        }
    }

    private void OnCancelClick(object? sender, RoutedEventArgs e) => Close();

    protected override void OnClosed(EventArgs e)
    {
        _poll.Stop();
        Page.EnvironmentRequested -= OnEnvironmentRequested;
        _result.TrySetResult(_signedIn);
        base.OnClosed(e);
    }
}
