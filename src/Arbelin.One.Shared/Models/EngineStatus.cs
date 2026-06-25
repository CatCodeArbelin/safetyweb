namespace Arbelin.One.Shared.Models;

public sealed record EngineStatus(
    string Name,
    bool IsInstalled,
    bool IsRunning,
    int? ProcessId,
    string Message
);
