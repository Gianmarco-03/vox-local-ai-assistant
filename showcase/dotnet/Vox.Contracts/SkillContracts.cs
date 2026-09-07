using System.Collections.Frozen;
using System.Text.Json;

namespace Vox.Contracts;

public enum Risk : byte
{
    ReadOnly = 0,
    Reversible = 1,
    Irreversible = 2,
}

public enum SkillLevel : byte
{
    L0,
    L1,
    L2,
}

public enum SkillLocation : byte
{
    Server,
    Device,
}

public enum JsonArgumentType : byte
{
    Text,
    WholeNumber,
    Numeric,
    Flag,
    Map,
    Sequence,
}

public enum ExecuteOutcome : byte
{
    Done,
    Denied,
    Cancelled,
    InvalidArguments,
    Unsupported,
    TimedOut,
    TransientFailure,
    PermanentFailure,
}

/// <summary>
/// Il CONTRATTO di una skill. Niente prosa: la descrizione che il modello
/// legge vive nell'ontologia (`loop/prompting.toml`) e la aggiunge l'host
/// quando pubblica il catalogo, cosi' il testo si corregge e si rimisura
/// senza ricompilare.
/// </summary>
public sealed record SkillDescriptor
{
    public required string SkillId { get; init; }
    public required string Name { get; init; }
    public FrozenDictionary<string, JsonArgumentType> Arguments { get; init; }
        = FrozenDictionary<string, JsonArgumentType>.Empty;
    public FrozenSet<string> RequiredArguments { get; init; }
        = FrozenSet<string>.Empty;
    public FrozenSet<string> Capabilities { get; init; }
        = FrozenSet<string>.Empty;
    public Risk Risk { get; init; } = Risk.Irreversible;
    public SkillLevel Level { get; init; } = SkillLevel.L1;
    public SkillLocation Location { get; init; } = SkillLocation.Device;
    public ushort ContractVersion { get; init; } = 1;

    public bool Destructive => Risk >= Risk.Irreversible;
}

public sealed record ExecuteSkillRequest(
    Guid CallId,
    string Skill,
    JsonElement Arguments,
    DateTimeOffset IssuedAt,
    DateTimeOffset Deadline,
    string DeviceId,
    string CatalogDigest,
    string IdempotencyKey,
    ushort ProtocolVersion = 1);

public sealed record SkillResult(
    bool Ok,
    string Speech,
    JsonElement? Data = null,
    bool Synthesize = false);

public sealed record SafeError(
    string Code,
    string UserMessage,
    bool Retryable = false);

public sealed record ExecuteSkillResult(
    Guid CallId,
    ExecuteOutcome Outcome,
    SkillResult? Result,
    SafeError? Error,
    bool Tainted,
    long LatencyMs,
    string HostVersion,
    string ContractDigest);

/// <summary>
/// Le capability che rendono NON FIDATO cio' che una skill restituisce.
/// Specchio di `fonti_non_fidate` in loop/capabilities.json — un test verifica
/// che i due elenchi coincidano, cosi' la prossima capability non fidata non
/// puo' essere dimenticata da una lista scritta a mano in un adapter.
/// </summary>
public static class TrustVocabulary
{
    public static readonly IReadOnlySet<string> UntrustedSources =
        new HashSet<string>(StringComparer.Ordinal)
        {
            "fs.read", "clipboard.read", "screen.ocr", "net",
            "net.search", "net.fetch", "messaging.read",
        };

    public static bool Taints(IEnumerable<string> capabilities) =>
        capabilities.Any(UntrustedSources.Contains);
}

