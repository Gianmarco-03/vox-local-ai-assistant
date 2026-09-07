using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Vox.Contracts;
using Vox.Platform;
using Vox.Prompting;
using Vox.SkillHost.Core;
using Vox.Skills.Compute;
using Vox.Skills.Desktop;
using Vox.Skills.Device;
using Vox.Skills.Files;
using Vox.Skills.Messaging;
using Vox.Skills.State;
using Vox.Skills.Productivity;
using Vox.Skills.System;
using Vox.Skills.Web;

namespace Vox.SkillHost;

internal static class Program
{
    public static async Task<int> Main(string[] args)
    {
        SkillRegistry registry = SkillHostBootstrap.BuildRegistry();

        PromptingCatalog prompting;
        try
        {
            prompting = SkillHostBootstrap.LoadPrompting(registry);
        }
        catch (PromptingCatalogException exception)
        {
            // Fallire, non degradare: un catalogo senza descrizioni arriverebbe
            // al modello come tool che nessuno sa quando usare, e un host che
            // parte lo stesso nasconde il guasto invece di dichiararlo.
            Console.Error.WriteLine($"ontologia: {exception.Message}");
            return 3;
        }

        switch (args)
        {
            case ["--health"]:
                Console.WriteLine(JsonSerializer.Serialize(new
                {
                    ok = true,
                    host_version = "0.1.0",
                    protocol_version = 1,
                    contract_digest = SkillHostBootstrap.ContractDigest(registry),
                    prompting_digest = SkillHostBootstrap.PromptingDigest(prompting),
                    skills = registry.Descriptors().Count,
                }));
                return 0;

            case ["--catalog"]:
                Console.WriteLine(JsonSerializer.Serialize(
                    SkillHostBootstrap.WireCatalog(registry, prompting),
                    SkillHostJsonContext.Default.IReadOnlyListWireSkillDescriptor));
                return 0;

            case ["--catalog-client"]:
                Console.WriteLine(JsonSerializer.Serialize(
                    SkillHostBootstrap.WireCatalog(registry, prompting)
                        .Where(skill => skill.Side == "client").ToArray(),
                    SkillHostJsonContext.Default.IReadOnlyListWireSkillDescriptor));
                return 0;

            case ["--prompting"]:
                Console.WriteLine(JsonSerializer.Serialize(
                    SkillHostBootstrap.WirePrompting(registry, prompting),
                    SkillHostJsonContext.Default.WirePrompting));
                return 0;

            case ["--self-test"]:
            {
                string state = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Vox", "skill-host");
                using var services = new ClientServiceProvider(Environment.CurrentDirectory, state);
                bool systemInfo = services.GetService(typeof(Vox.Skills.System.ISystemInfoService)) is not null;
                bool desktop = services.GetService(typeof(Vox.Skills.Desktop.IDesktopService)) is not null;
                bool timers = services.GetService(typeof(Vox.Skills.Productivity.ITimerScheduler)) is not null;
                Console.WriteLine(JsonSerializer.Serialize(new { ok = systemInfo && timers, system_info = systemInfo, desktop, timers, platform = Environment.OSVersion.Platform.ToString() }));
                return systemInfo && timers ? 0 : 1;
            }

            case ["--stdio"]:
            {
                string state = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Vox", "skill-host");
                return await StdioSkillHost.RunAsync(registry, prompting, Environment.CurrentDirectory, state, CancellationToken.None).ConfigureAwait(false);
            }

            case ["--stdio-server", var serverConfig]:
            {
                // Il tubo del SERVER: stesso protocollo del client, catalogo
                // `side: server`, servizi cablati dalla config del demone.
                using var services = new ServerServiceProvider(serverConfig, prompting);
                return await StdioSkillHost.RunAsync(
                    registry, prompting, "server", Vox.Contracts.SkillLocation.Server,
                    services, CancellationToken.None).ConfigureAwait(false);
            }

            default:
                Console.Error.WriteLine(
                    "Uso: vox-skill-host --health | --catalog | --catalog-client | --prompting | --self-test | --stdio | --stdio-server <config.json>");
                return 2;
        }
    }
}

public static class SkillHostBootstrap
{
    private static readonly HashSet<string> KnownCapabilities =
        new(StringComparer.Ordinal) { "app.launch", "audio.app", "audio.system", "clipboard.read", "clipboard.write", "compute.numeric", "fs.read", "fs.write", "fs.trash", "input.synth", "media.control", "messaging.read", "messaging.send", "net", "net.fetch", "net.search", "notify", "panel.show", "power", "scheduler", "screen.capture", "screen.ocr", "state.read", "state.write", "system.info", "window.control", "window.read" };

    public static SkillRegistry BuildRegistry()
    {
        SkillRegistry registry = new(KnownCapabilities);
        registry.Add(new CalculateSkill());
        registry.Add(new OpenAppSkill());
        registry.Add(new WindowListSkill());
        registry.Add(new WindowControlSkill());
        registry.Add(new CloseWindowSkill());
        registry.Add(new ClipboardReadSkill());
        registry.Add(new ClipboardWriteSkill());
        registry.Add(new OpenResourceSkill());
        registry.Add(new SetVolumeSkill());
        registry.Add(new MediaControlSkill());
        registry.Add(new ScreenSkill());
        registry.Add(new PowerControlSkill());
        registry.Add(new MessageListChatsSkill());
        registry.Add(new MessageReadSkill());
        registry.Add(new MessageGetUnreadSkill());
        registry.Add(new ChatSummarySkill());
        registry.Add(new MessageComposeSkill());
        registry.Add(new MessageSendSkill());
        registry.Add(new MessageReplySkill());
        registry.Add(new WebSearchSkill());
        registry.Add(new WebResearchSkill());
        registry.Add(new WebExploreSkill());
        registry.Add(new CurrentTimeSkill());
        registry.Add(new ListDirectorySkill());
        registry.Add(new FileSearchSkill());
        registry.Add(new MoveFileSkill());
        registry.Add(new TrashFileSkill());
        registry.Add(new MemoryReadSkill());
        registry.Add(new MemoryWriteSkill());
        registry.Add(new ConnectMachineSkill());
        registry.Add(new ReasoningModeSkill());
        registry.Add(new ShowPanelSkill());
        registry.Add(new TimerSkill());
        registry.Add(new ReminderSkill());
        registry.Add(new SystemInfoSkill());
        return registry;
    }

    /// <summary>
    /// Carica l'ontologia e verifica che combaci col registro. Il controllo sta
    /// all'avvio e non in un lint a parte: una skill senza voce non deve poter
    /// arrivare al modello nemmeno una volta.
    /// </summary>
    public static PromptingCatalog LoadPrompting(SkillRegistry registry)
    {
        string path = PromptingCatalog.Resolve()
            ?? throw new PromptingCatalogException(
                "loop/prompting.toml non trovata (cercata in VOX_PROMPTING, accanto al "
                + "binario e risalendo dall'eseguibile).");

        PromptingCatalog catalog = PromptingCatalog.Load(path);
        catalog.Validate(registry.Descriptors().Select(descriptor => descriptor.Name));
        return catalog;
    }

    /// <summary>
    /// Impronta del CONTRATTO: nomi, argomenti, capability, rischio, livello,
    /// side. Non comprende la prosa, cosi' correggere una descrizione non
    /// invalida le sessioni aperte ne' le autorizzazioni gia' concesse.
    /// </summary>
    public static string ContractDigest(SkillRegistry registry) =>
        ContractDigest(registry.Descriptors());

    public static string ContractDigest(IEnumerable<SkillDescriptor> descriptors)
    {
        StringBuilder builder = new();
        foreach (SkillDescriptor descriptor in descriptors)
        {
            builder.Append(descriptor.SkillId).Append('|')
                .Append(descriptor.Name).Append('|')
                .Append(string.Join(",", descriptor.Arguments
                    .OrderBy(pair => pair.Key, StringComparer.Ordinal)
                    .Select(pair => $"{pair.Key}:{ArgumentTypeName(pair.Value)}"))).Append('|')
                .Append(string.Join(",", descriptor.RequiredArguments.Order(StringComparer.Ordinal)))
                .Append('|')
                .Append(string.Join(",", descriptor.Capabilities.Order(StringComparer.Ordinal)))
                .Append('|')
                .Append(descriptor.Risk).Append('|')
                .Append(descriptor.Level).Append('|')
                .Append(descriptor.Location).Append('\n');
        }

        return Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(builder.ToString())));
    }

    /// <summary>Impronta del TESTO, separata: cambia quando si accorda il prompt.</summary>
    public static string PromptingDigest(PromptingCatalog prompting)
    {
        StringBuilder builder = new();
        builder.Append(prompting.Version).Append('\n');
        foreach ((string name, SkillPrompting entry) in prompting.Skills
            .OrderBy(pair => pair.Key, StringComparer.Ordinal))
        {
            builder.Append(name).Append('|').Append(entry.Description).Append('|')
                .Append(entry.RouterLine ?? "").Append('|')
                .Append(string.Join("¶", entry.Phrases)).Append('|')
                .Append(string.Join("¶", entry.CounterPhrases)).Append('|')
                .Append(entry.Synthesis ?? "").Append('\n');
        }

        foreach (RouterExample example in prompting.Examples)
        {
            builder.Append(example.Utterance).Append("=>").Append(example.Tool).Append('\n');
        }

        return Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(builder.ToString())));
    }

    public static IReadOnlyList<WireSkillDescriptor> WireCatalog(
        SkillRegistry registry, PromptingCatalog prompting) => registry.Descriptors()
        .Select(descriptor => WireSkillDescriptor.FromDomain(
            descriptor, prompting.Skills[descriptor.Name].Description))
        .ToArray();

    public static WirePrompting WirePrompting(
        SkillRegistry registry, PromptingCatalog prompting)
    {
        Dictionary<string, WireSkillPrompting> skills = new(StringComparer.Ordinal);
        foreach (SkillDescriptor descriptor in registry.Descriptors())
        {
            SkillPrompting entry = prompting.Skills[descriptor.Name];
            skills[descriptor.Name] = new WireSkillPrompting(
                entry.Description,
                entry.EffectiveRouterLine,
                entry.Phrases,
                entry.CounterPhrases,
                entry.Synthesis);
        }

        return new WirePrompting(
            prompting.Version,
            PromptingDigest(prompting),
            skills,
            prompting.Examples
                .Select(example => new WireRouterExample(example.Utterance, example.Tool))
                .ToArray());
    }

    internal static string ArgumentTypeName(JsonArgumentType type) => type switch
    {
        JsonArgumentType.Text => "str",
        JsonArgumentType.WholeNumber => "int",
        JsonArgumentType.Numeric => "float",
        JsonArgumentType.Flag => "bool",
        JsonArgumentType.Map => "dict",
        JsonArgumentType.Sequence => "list",
        _ => throw new ArgumentOutOfRangeException(nameof(type)),
    };
}

public sealed record WireSkillDescriptor(
    string SkillId,
    string Name,
    string Description,
    IReadOnlyDictionary<string, string> Args,
    IReadOnlyList<string> RequiredArgs,
    IReadOnlyList<string> Capabilities,
    string Risk,
    string Level,
    string Side)
{
    public static WireSkillDescriptor FromDomain(SkillDescriptor descriptor, string description) => new(
        descriptor.SkillId,
        descriptor.Name,
        description,
        descriptor.Arguments.ToDictionary(
            pair => pair.Key,
            pair => SkillHostBootstrap.ArgumentTypeName(pair.Value),
            StringComparer.Ordinal),
        descriptor.RequiredArguments.Order(StringComparer.Ordinal).ToArray(),
        descriptor.Capabilities.Order(StringComparer.Ordinal).ToArray(),
        descriptor.Risk switch
        {
            Vox.Contracts.Risk.ReadOnly => "readonly",
            Vox.Contracts.Risk.Reversible => "reversible",
            Vox.Contracts.Risk.Irreversible => "irreversible",
            _ => throw new ArgumentOutOfRangeException(nameof(descriptor)),
        },
        descriptor.Level.ToString(),
        descriptor.Location is SkillLocation.Server ? "server" : "client");
}

public sealed record WireSkillPrompting(
    string Description,
    string RouterLine,
    IReadOnlyList<string> Phrases,
    IReadOnlyList<string> CounterPhrases,
    string? Synthesis);

public sealed record WireRouterExample(string Utterance, string Tool);

public sealed record WirePrompting(
    int Version,
    string Digest,
    IReadOnlyDictionary<string, WireSkillPrompting> Skills,
    IReadOnlyList<WireRouterExample> Examples);

[JsonSourceGenerationOptions(
    PropertyNamingPolicy = JsonKnownNamingPolicy.SnakeCaseLower,
    UseStringEnumConverter = true,
    WriteIndented = true)]
[JsonSerializable(typeof(IReadOnlyList<WireSkillDescriptor>))]
[JsonSerializable(typeof(WirePrompting))]
internal sealed partial class SkillHostJsonContext : JsonSerializerContext;
