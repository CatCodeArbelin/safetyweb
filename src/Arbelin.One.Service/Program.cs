using Arbelin.One.Service;
using Serilog;

Log.Logger = new LoggerConfiguration().WriteTo.Console().CreateLogger();

try
{
    Log.Information("Arbelin One Service bootstrap started.");
    var builder = Host.CreateApplicationBuilder(args);
    builder.Services.AddSerilog();
    builder.Services.AddHostedService<Worker>();
    var host = builder.Build();
    host.Run();
}
finally
{
    Log.CloseAndFlush();
}
