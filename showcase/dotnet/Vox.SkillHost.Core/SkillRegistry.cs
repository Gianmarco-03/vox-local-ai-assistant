using Vox.Contracts;
using Vox.Skill.Abstractions;

namespace Vox.SkillHost.Core;

public sealed class SkillRegistry
{
    private readonly Dictionary<string, ISkill> _skills = new(StringComparer.Ordinal);
    private readonly IReadOnlySet<string> _knownCapabilities;

    public SkillRegistry(IReadOnlySet<string> knownCapabilities)
    {
        _knownCapabilities = knownCapabilities;
    }

    public void Add(ISkill skill)
    {
        ArgumentNullException.ThrowIfNull(skill);
        SkillDescriptor descriptor = skill.Descriptor;

        if (descriptor.Level is SkillLevel.L0 && descriptor.Destructive)
        {
            throw new InvalidOperationException(
                $"Skill '{descriptor.SkillId}': L0 e distruttiva non possono coesistere.");
        }

        string[] unknown = descriptor.Capabilities
            .Where(capability => !_knownCapabilities.Contains(capability))
            .Order(StringComparer.Ordinal)
            .ToArray();
        if (unknown.Length > 0)
        {
            throw new InvalidOperationException(
                $"Skill '{descriptor.SkillId}' dichiara capability sconosciute: {string.Join(", ", unknown)}.");
        }

        if (!_skills.TryAdd(descriptor.Name, skill))
        {
            throw new InvalidOperationException($"Skill duplicata: {descriptor.Name}.");
        }
    }

    public ISkill? Get(string name) => _skills.GetValueOrDefault(name);

    public IReadOnlyList<SkillDescriptor> Descriptors() => _skills.Values
        .Select(skill => skill.Descriptor)
        .OrderBy(descriptor => descriptor.Name, StringComparer.Ordinal)
        .ToArray();
}
