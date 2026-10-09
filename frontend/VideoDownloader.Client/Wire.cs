namespace VideoDownloader.Client;

/// <summary>A job as the core describes it. Values only - nothing live travels.</summary>
/// <remarks>
/// The field names here follow the wire, which follows `JobSnapshot` on the
/// Python side. Two of them are worth reading twice:
/// <list type="bullet">
/// <item><c>HasKnownTotal</c> false means the end is unknown, <b>not</b> that
/// nothing is done. A bar must go indeterminate here, not to zero.</item>
/// <item><c>Done</c>/<c>Total</c> are counted in <c>Unit</c>, which is either
/// segments or bytes. The core used to call both of them "segments" and carry
/// bytes in them; the unit travels now so the front end never has to guess.</item>
/// </list>
/// </remarks>
public sealed record JobSnapshot(
    string Id,
    string? Url,
    string? Title,
    string State,
    double Progress,
    long Done,
    long Total,
    string Unit,
    bool HasKnownTotal,
    string? OutputFile,
    long? ExpectedBytes,
    JobFailure? Error,
    bool FromDisk,
    long Seq)
{
    /// <summary>What to show for a job, in the core's order of preference.</summary>
    public string DisplayTitle =>
        !string.IsNullOrWhiteSpace(Title) ? Title!
        : !string.IsNullOrWhiteSpace(Url) ? Url!
        : "Vorhandene Datei";
}

/// <summary>
/// A failure as a value, with the classification the core already made.
/// </summary>
/// <remarks>
/// <c>Text</c> is the sentence the old window used to show; <c>Kind</c> is what
/// a program should branch on. The exception behind the failure stays in the
/// core's log and never travels - it is a live Python object with no business
/// on a wire.
/// </remarks>
public sealed record JobFailure(string Kind, string Code, string Message, bool Retryable, string Text);

/// <summary>The core asking whether to start a download it considers large.</summary>
public sealed record LargeDownloadAsk(string AskId, string JobId, string? Title, long? EstimatedBytes);

/// <summary>A site a login can be shown for, as the core names it.</summary>
public sealed record SiteLogin(string Site, string LoginUrl, IReadOnlyList<string> RequiredCookies);

/// <summary>
/// A login the core is watching: the page to show, and the id to report its
/// cookies under. Started by the user (<c>login.start</c>) or by a job
/// (<c>ask.login</c>) - from here on the two are the same.
/// </summary>
public sealed record LoginPage(string LoginId, string Site, string LoginUrl, IReadOnlyList<string> RequiredCookies);

/// <summary>The core asking for a site login it cannot perform itself.</summary>
/// <remarks>
/// Answered by showing <see cref="Page"/> and reporting its cookies, not by
/// sending cookies back: the core decides when the login is finished.
/// </remarks>
public sealed record LoginAsk(string AskId, LoginPage Page);

/// <summary>A login ended on the core's side.</summary>
/// <param name="SignedIn">True once the session is stored; false when the core gave up on it.</param>
/// <param name="Persisted">Whether the session outlives this run of the application.</param>
public sealed record LoginOutcome(string LoginId, string Site, bool SignedIn, bool Persisted);

/// <summary>One cookie a login page holds, reported to the core.</summary>
/// <remarks>
/// The core picks the ones it needs and builds the session from them; a front
/// end never touches the session store, and cannot store a session for a site it
/// was not asked about. Values are credentials: they are never logged, here or
/// anywhere.
/// </remarks>
public sealed record SessionCookie(string Name, string Value, string Domain)
{
    public override string ToString() => $"SessionCookie {{ Name = {Name}, Domain = {Domain}, Value = <redacted> }}";
}

/// <summary>What the core knows about site sessions, and which sites a login can be started for.</summary>
public sealed record SessionOverview(IReadOnlyList<string> Sites, bool Persists, IReadOnlyList<SiteLogin> Logins);
