using System.Windows;

namespace Arbelin.One.Client;

public partial class MainWindow : Window
{
    private const string StubMessage = "PR-01 bootstrap: network logic is not implemented yet.";

    public MainWindow()
    {
        InitializeComponent();
    }

    private void OnStubAction(object sender, RoutedEventArgs e)
    {
        MockLog.Items.Add(StubMessage);
    }
}
