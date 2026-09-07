import Foundation

/// Chi può eseguire cosa, e quando serve una conferma. Gemello di
/// `brain/policy.py`, principi identici perché sono gli stessi principi:
/// allowlist mai blocklist, conferma **riletta** per ogni azione irreversibile,
/// il contenuto letto è dato, un controllo che fallisce non concede.
public enum PolicyOutcome: Sendable, Equatable {
    case allow
    case needsConfirmation
    case deny
}

public struct PolicyDecision: Sendable, Equatable {
    public let outcome: PolicyOutcome
    public let reason: String
    public let readback: String        // la frase da rileggere PRIMA di eseguire

    public init(_ outcome: PolicyOutcome, reason: String = "", readback: String = "") {
        self.outcome = outcome
        self.reason = reason
        self.readback = readback
    }
}

public struct Policy: Sendable {
    public let registry: SkillRegistry
    public let threshold: Risk
    public let vocabulary: Capabilities

    public init(registry: SkillRegistry, threshold: Risk = .irreversible,
                vocabulary: Capabilities = .bundled) {
        self.registry = registry
        self.threshold = threshold
        self.vocabulary = vocabulary
    }

    public func judge(_ tool: String, _ args: [String: JSONValue]) -> PolicyDecision {
        guard let spec = registry.get(tool) else {
            // Allowlist: ciò che non è dichiarato non è permesso.
            return PolicyDecision(.deny, reason: "skill '\(tool)' non registrata")
        }
        guard registry.validateArgs(tool, args) else {
            return PolicyDecision(.deny, reason: "argomenti non conformi al contratto di '\(tool)'")
        }
        if spec.requiresConfirmation(vocabulary) || spec.risk >= threshold {
            // CR-03: se lo spec dichiara argomenti e la chiamata non ne porta
            // nessuno, la rilettura non potrebbe MAI nominare l'azione
            // concreta. Si nega, invece di mostrare una frase generica che
            // l'utente potrebbe scambiare per specifica.
            if args.isEmpty && !spec.args.isEmpty {
                return PolicyDecision(.deny, reason:
                    "conferma non specifica rifiutata: lo spec dichiara argomenti ma questa "
                    + "chiamata non ne porta nessuno, la rilettura non potrebbe nominare l'azione")
            }
            return PolicyDecision(.needsConfirmation, reason: spec.risk.wireName,
                                  readback: Self.readback(spec, args))
        }
        return PolicyDecision(.allow)
    }

    /// L'azione riletta all'utente. Una conferma su «vuoi procedere?» non è una
    /// conferma: l'utente deve sentire COSA sta autorizzando.
    public static func readback(_ spec: SkillSpec, _ args: [String: JSONValue]) -> String {
        let details = args.keys.sorted()
            .compactMap { key -> String? in
                guard let value = args[key], value != .null else { return nil }
                return "\(key) \(Self.plain(value))"
            }
            .joined(separator: ", ")
        return details.isEmpty
            ? "\(spec.description). Confermi?"
            : "\(spec.description). \(details). Confermi?"
    }

    private static func plain(_ value: JSONValue) -> String {
        switch value {
        case .string(let v): return v
        case .int(let v): return String(v)
        case .double(let v): return String(v)
        case .bool(let v): return v ? "true" : "false"
        default:
            let data = (try? JSONEncoder().encode(value)) ?? Data()
            return String(decoding: data, as: UTF8.self)
        }
    }

    /// True se l'esito di questa skill è testo scritto da qualcun altro.
    public func taintsContext(_ tool: String) -> Bool {
        guard let spec = registry.get(tool) else { return false }
        return vocabulary.readsUntrusted(spec.capabilities)
    }
}
