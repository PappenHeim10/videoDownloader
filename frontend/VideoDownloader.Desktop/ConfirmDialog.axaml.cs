using Avalonia.Controls;

namespace VideoDownloader.Desktop;

/// <summary>
/// A question with a few named answers. Closing the window answers -1, which
/// every caller reads as the cautious choice.
/// </summary>
public partial class ConfirmDialog : Window
{
    public ConfirmDialog()
    {
        InitializeComponent();
    }

    /// <param name="message">The question, in full.</param>
    /// <param name="answers">Button labels, left to right. The result is the index of the one clicked.</param>
    /// <param name="defaultAnswer">The answer Enter gives - always the cautious one.</param>
    public ConfirmDialog(string message, IReadOnlyList<string> answers, int defaultAnswer) : this()
    {
        MessageText.Text = message;
        for (var index = 0; index < answers.Count; index++)
        {
            var answer = index;
            var button = new Button { Content = answers[index], IsDefault = index == defaultAnswer };
            button.Click += (_, _) => Close(answer);
            Buttons.Children.Add(button);
        }
    }
}
