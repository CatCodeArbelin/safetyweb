using Arbelin.One.Shared.Models;

namespace Arbelin.One.Tests;

public sealed class BootstrapTests
{
    [Fact]
    public void AppModeStoppedExists()
    {
        Assert.Equal(0, (int)AppMode.Stopped);
    }

    [Fact]
    public void EngineStatusCanBeCreated()
    {
        var status = new EngineStatus("xray", IsInstalled: false, IsRunning: false, ProcessId: null, "not configured");
        Assert.Equal("xray", status.Name);
        Assert.False(status.IsInstalled);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void EngineStatusStoresIsRunning(bool isRunning)
    {
        var status = new EngineStatus("zapret", IsInstalled: true, isRunning, ProcessId: 1234, "mock");
        Assert.Equal(isRunning, status.IsRunning);
    }
}
