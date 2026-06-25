using System.Windows;
using Serilog;

namespace Arbelin.One.Client;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        Log.Logger = new LoggerConfiguration().WriteTo.File("logs/arbelin-one-client.log", rollingInterval: RollingInterval.Day).CreateLogger();
        Log.Information("Arbelin One Client bootstrap started.");
        base.OnStartup(e);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        Log.CloseAndFlush();
        base.OnExit(e);
    }
}
