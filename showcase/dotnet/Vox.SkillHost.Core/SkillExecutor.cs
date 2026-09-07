using System.Diagnostics;
using Vox.Contracts;
using Vox.Skill.Abstractions;

namespace Vox.SkillHost.Core;

public sealed class SkillExecutor
{
    private const string HostVersion = "0.1.0";
    private readonly SkillRegistry _registry;
    private readonly IReadOnlySet<string> _accepted;
    private readonly IPolicyEvaluator _policy;
    private readonly IConfirmationService _confirmation;
    private readonly IIdempotencyStore _idempotency;
    private readonly ICatalogDigest _catalog;
    private readonly TimeProvider _timeProvider;
    private readonly IServiceProvider _services;
    private readonly string _deviceId;

    public SkillExecutor(
        SkillRegistry registry,
        IReadOnlySet<string> accepted,
        IPolicyEvaluator policy,
        IConfirmationService confirmation,
        IIdempotencyStore idempotency,
        ICatalogDigest catalog,
        TimeProvider timeProvider,
        IServiceProvider services,
        string deviceId)
    {
        _registry = registry;
        _accepted = accepted;
        _policy = policy;
        _confirmation = confirmation;
        _idempotency = idempotency;
        _catalog = catalog;
        _timeProvider = timeProvider;
        _services = services;
        _deviceId = deviceId;
    }

    public async ValueTask<ExecuteSkillResult> ExecuteAsync(
        ExecuteSkillRequest request,
        CancellationToken cancellationToken = default)
    {
        long started = Stopwatch.GetTimestamp();
        ExecuteSkillResult Finish(
            ExecuteOutcome outcome,
            SkillResult? result = null,
            SafeError? error = null,
            bool tainted = false) =>
            new(
                request.CallId,
                outcome,
                result,
                error,
                tainted,
                checked((long)Stopwatch.GetElapsedTime(started).TotalMilliseconds),
                HostVersion,
                _catalog.Current);

        DateTimeOffset now = _timeProvider.GetUtcNow();
        if (request.ProtocolVersion != 1 || request.DeviceId != _deviceId)
        {
            return Finish(ExecuteOutcome.Denied, error: Denied("request_not_accepted"));
        }

        if (request.Deadline <= now)
        {
            return Finish(ExecuteOutcome.TimedOut, error: Denied("deadline_expired"));
        }

        if (!StringComparer.Ordinal.Equals(request.CatalogDigest, _catalog.Current))
        {
            return Finish(ExecuteOutcome.Denied, error: Denied("catalog_mismatch"));
        }

        ISkill? skill = _registry.Get(request.Skill);
        if (skill is null || !_accepted.Contains(request.Skill))
        {
            return Finish(ExecuteOutcome.Unsupported, error: Denied("skill_not_accepted"));
        }

        try
        {
            skill.ValidateArguments(request.Arguments);
        }
        catch (InvalidSkillArgumentsException)
        {
            return Finish(
                ExecuteOutcome.InvalidArguments,
                error: Denied("invalid_arguments"));
        }

        PolicyDecision decision = _policy.Evaluate(skill.Descriptor, request);
        if (decision.Outcome is PolicyOutcome.Deny)
        {
            return Finish(ExecuteOutcome.Denied, error: Denied("policy_denied"));
        }

        if (decision.Outcome is PolicyOutcome.NeedsConfirmation)
        {
            ConfirmationAnswer answer = await _confirmation
                .ConfirmAsync(decision.Readback, request.Deadline, cancellationToken)
                .ConfigureAwait(false);
            if (answer is not ConfirmationAnswer.Yes)
            {
                ExecuteOutcome outcome = answer is ConfirmationAnswer.No or ConfirmationAnswer.TimedOut
                    ? ExecuteOutcome.Cancelled
                    : ExecuteOutcome.Denied;
                return Finish(outcome, error: Denied("confirmation_not_obtained"));
            }
        }

        if (!await _idempotency
                .TryAcquireAsync(request.IdempotencyKey, request.Deadline, cancellationToken)
                .ConfigureAwait(false))
        {
            return Finish(ExecuteOutcome.Denied, error: Denied("duplicate_request"));
        }

        using CancellationTokenSource deadline = CancellationTokenSource.CreateLinkedTokenSource(
            cancellationToken);
        // Una scadenza oltre il tetto dei timer (~24 giorni) farebbe esplodere
        // CancelAfter: una deadline lontana equivale a nessuna fretta.
        TimeSpan remaining = request.Deadline - now;
        if (remaining < TimeSpan.FromMilliseconds(int.MaxValue))
        {
            deadline.CancelAfter(remaining);
        }

        try
        {
            SkillContext context = new(_deviceId, _timeProvider, _services);
            SkillResult result = await skill
                .ExecuteAsync(context, request.Arguments, deadline.Token)
                .ConfigureAwait(false);
            return Finish(
                result.Ok ? ExecuteOutcome.Done : ExecuteOutcome.PermanentFailure,
                result,
                tainted: _policy.TaintsContext(skill.Descriptor));
        }
        catch (InvalidSkillArgumentsException)
        {
            return Finish(
                ExecuteOutcome.InvalidArguments,
                error: Denied("invalid_arguments"));
        }
        catch (OperationCanceledException) when (deadline.IsCancellationRequested)
        {
            return Finish(ExecuteOutcome.TimedOut, error: Denied("execution_timed_out"));
        }
        catch (Exception)
        {
            return Finish(
                ExecuteOutcome.PermanentFailure,
                error: new SafeError("execution_failed", "L'azione non è riuscita."));
        }
    }

    private static SafeError Denied(string code) =>
        new(code, "Non posso eseguire questa azione su questo dispositivo.");
}
