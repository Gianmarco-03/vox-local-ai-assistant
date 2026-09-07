using System.Collections.Frozen;
using System.Text.Json;
using Vox.Contracts;
using Vox.Skill.Abstractions;
using Vox.SkillHost.Core;
using Vox.Skills.System;
using Xunit;

namespace Vox.SkillHost.Tests;

public sealed class SkillExecutorTests
{
    private static readonly DateTimeOffset Now =
        new(2026, 9, 1, 12, 0, 0, TimeSpan.Zero);
    private static readonly FrozenSet<string> AcceptedSkills =
        new[] { "current_time" }.ToFrozenSet(StringComparer.Ordinal);

    [Fact]
    public async Task CurrentTimeExecutesThroughTheRealRegistry()
    {
        SkillExecutor executor = CreateExecutor();

        ExecuteSkillResult result = await executor.ExecuteAsync(
            Request(), TestContext.Current.CancellationToken);

        Assert.Equal(ExecuteOutcome.Done, result.Outcome);
        Assert.True(result.Result?.Ok);
        Assert.Equal("È mezzogiorno in punto", result.Result?.Speech);
        Assert.Equal("12:00", result.Result?.Data?.GetProperty("clock").GetString());
        Assert.False(result.Tainted);
    }

    [Fact]
    public async Task UnknownArgumentsAreRejected()
    {
        SkillExecutor executor = CreateExecutor();

        ExecuteSkillResult result = await executor.ExecuteAsync(
            Request(JsonSerializer.SerializeToElement(new { unexpected = true })),
            TestContext.Current.CancellationToken);

        Assert.Equal(ExecuteOutcome.InvalidArguments, result.Outcome);
    }

    [Fact]
    public async Task CatalogMismatchFailsClosed()
    {
        SkillExecutor executor = CreateExecutor();
        ExecuteSkillRequest request = Request() with { CatalogDigest = "wrong" };

        ExecuteSkillResult result = await executor.ExecuteAsync(
            request, TestContext.Current.CancellationToken);

        Assert.Equal(ExecuteOutcome.Denied, result.Outcome);
        Assert.Equal("catalog_mismatch", result.Error?.Code);
    }

    [Fact]
    public async Task DuplicateIdempotencyKeyIsRejected()
    {
        SkillExecutor executor = CreateExecutor();
        ExecuteSkillRequest request = Request();

        Assert.Equal(
            ExecuteOutcome.Done,
            (await executor.ExecuteAsync(request, TestContext.Current.CancellationToken)).Outcome);
        Assert.Equal(
            ExecuteOutcome.Denied,
            (await executor.ExecuteAsync(request, TestContext.Current.CancellationToken)).Outcome);
    }

    private static ExecuteSkillRequest Request(JsonElement? arguments = null) => new(
        Guid.NewGuid(),
        "current_time",
        arguments ?? JsonSerializer.SerializeToElement(new { }),
        Now,
        Now.AddSeconds(5),
        "test-device",
        "catalog-v1",
        "idem-1");

    private static SkillExecutor CreateExecutor()
    {
        SkillRegistry registry = new(FrozenSet<string>.Empty);
        registry.Add(new CurrentTimeSkill());
        return new SkillExecutor(
            registry,
            AcceptedSkills,
            new AllowPolicy(),
            new NoConfirmation(),
            new MemoryIdempotency(),
            new CatalogDigest(),
            new FixedTimeProvider(Now),
            new EmptyServices(),
            "test-device");
    }

    private sealed class AllowPolicy : IPolicyEvaluator
    {
        public PolicyDecision Evaluate(SkillDescriptor descriptor, ExecuteSkillRequest request) =>
            new(PolicyOutcome.Allow);

        public bool TaintsContext(SkillDescriptor descriptor) => false;
    }

    private sealed class NoConfirmation : IConfirmationService
    {
        public ValueTask<ConfirmationAnswer> ConfirmAsync(
            string readback,
            DateTimeOffset deadline,
            CancellationToken cancellationToken) =>
            ValueTask.FromResult(ConfirmationAnswer.Unavailable);
    }

    private sealed class MemoryIdempotency : IIdempotencyStore
    {
        private readonly HashSet<string> _keys = new(StringComparer.Ordinal);

        public ValueTask<bool> TryAcquireAsync(
            string key,
            DateTimeOffset expiresAt,
            CancellationToken cancellationToken) =>
            ValueTask.FromResult(_keys.Add(key));
    }

    private sealed class CatalogDigest : ICatalogDigest
    {
        public string Current => "catalog-v1";
    }

    private sealed class FixedTimeProvider(DateTimeOffset now) : TimeProvider
    {
        public override DateTimeOffset GetUtcNow() => now;
        public override TimeZoneInfo LocalTimeZone => TimeZoneInfo.Utc;
    }

    private sealed class EmptyServices : IServiceProvider
    {
        public object? GetService(Type serviceType) => null;
    }
}
