using System.Globalization;

namespace VideoDownloader.Desktop;

/// <summary>Numbers as the German window writes them: one decimal, decimal comma.</summary>
/// <remarks>
/// Not <c>CultureInfo("de-DE")</c>: the front end builds with
/// <c>InvariantGlobalization</c>, where asking for any named culture throws -
/// in the first progress update rather than at start-up. A decimal comma is the
/// only part of the culture this window needs.
/// </remarks>
internal static class Numbers
{
    private static readonly NumberFormatInfo DecimalComma = new() { NumberDecimalSeparator = "," };

    public static string OneDecimal(double value) => value.ToString("0.0", DecimalComma);
}
