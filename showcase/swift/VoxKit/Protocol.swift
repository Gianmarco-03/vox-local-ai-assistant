import Foundation

/// Il protocollo client ↔ server v1, lato client Apple.
///
/// Contratto: `docs/PROTOCOLLO-CLIENT-SERVER.md`. Questo file è il gemello
/// Swift di `remote/protocol.py`: stessi limiti, stesso vocabolario chiuso,
/// stesso principio 4 — **fallire chiuso**. Ogni `decode` solleva invece di
/// indovinare: un messaggio che non si capisce non è un messaggio da eseguire.
public enum VoxProtocol {
    public static let version = "1"

    // §8. Limiti per messaggio. Sono del protocollo, non di questa
    // implementazione: non si allargano qui.
    public static let maxJSONBytes = 64 * 1024
    public static let maxTextBytes = 4 * 1024        // `speech` e `error`
    public static let maxDataBytes = 32 * 1024
    public static let dataTooLarge = "risultato troppo grande"

    // §5. Vocabolario chiuso degli esiti. `done` è l'unico che dice «è successo».
    public static let outcomes: Set<String> = ["done", "denied", "cancelled", "timeout", "error"]

    // §8. `unauthorized` e `protocolUnsupported` chiudono la sessione; `malformed` no.
    public static let errorCodes: Set<String> = ["malformed", "unauthorized", "protocol_unsupported"]

    // §2. Cosa un dispositivo sa fare (vocabolario diverso da quello delle skill).
    public static let clientCapabilities: Set<String> = ["capture", "playback", "confirm", "cockpit"]

    // §2 + §5 + §7. I messaggi che il server può mandare: allowlist, mai
    // blocklist. Una chiave fuori da qui è `malformed`, non un'estensione da
    // tollerare.
    public static let serverMessages: Set<String> = [
        "welcome", "error", "execute", "snapshot",
        "speak", "speak_done", "abort",
        "state", "rms", "line", "vox_out", "agent_update", "clear", "commands",
    ]

    // §3. I tipi degli argomenti sul filo.
    public static let argumentTypes: Set<String> = ["int", "str", "float", "bool", "dict", "list"]
}

public struct ProtocolError: Error, CustomStringConvertible, Equatable {
    public let description: String
    public let code: String
    public let ref: String?

    public init(_ description: String, code: String = "malformed", ref: String? = nil) {
        self.description = description
        self.code = VoxProtocol.errorCodes.contains(code) ? code : "malformed"
        self.ref = ref
    }
}

// MARK: - §3 catalogo

public extension VoxProtocol {
    /// `SkillSpec` → JSON di §3. Serializzazione 1:1, nessun campo inventato.
    static func wire(_ spec: SkillSpec) throws -> JSONValue {
        let unknown = spec.args.values.filter { !argumentTypes.contains($0) }
        guard unknown.isEmpty else {
            throw ProtocolError("skill '\(spec.skillID)': tipo di argomento non serializzabile \(unknown.sorted())")
        }
        return .object([
            "skill_id": .string(spec.skillID),
            "name": .string(spec.name),
            "description": .string(spec.description),
            "args": .object(spec.args.mapValues { .string($0) }),
            "required_args": .array(spec.requiredArgs.map { .string($0) }),
            "capabilities": .array(spec.capabilities.map { .string($0) }),
            "risk": .string(spec.risk.wireName),
            "level": .string(spec.level),
        ])
    }

    /// JSON di §3 → `SkillSpec`.
    ///
    /// Sul filo vero il client solo serializza: il verso opposto lo fa il
    /// `RemoteSkillRegistry` del server. Vive qui perché è ciò che rende
    /// dimostrabile, con un test di andata e ritorno, che §3 non perde niente.
    static func spec(from payload: JSONValue) throws -> SkillSpec {
        guard let object = payload.objectValue else {
            throw ProtocolError("SkillSpec: atteso un oggetto JSON")
        }
        func required(_ key: String) throws -> String {
            guard let value = object[key]?.stringValue else {
                throw ProtocolError("SkillSpec: campo obbligatorio assente o non testuale: '\(key)'")
            }
            return value
        }
        guard let argsObject = object["args"]?.objectValue else {
            throw ProtocolError("SkillSpec.args: atteso un oggetto JSON")
        }
        var args: [String: String] = [:]
        for (key, value) in argsObject {
            guard let name = value.stringValue, argumentTypes.contains(name) else {
                throw ProtocolError("SkillSpec.args: tipo non nel vocabolario per '\(key)'")
            }
            args[key] = name
        }
        let riskName = object["risk"]?.stringValue ?? "irreversible"
        guard let risk = Risk(wireName: riskName) else {
            throw ProtocolError("SkillSpec.risk sconosciuto: '\(riskName)'")
        }
        return SkillSpec(
            skillID: try required("skill_id"),
            name: try required("name"),
            description: try required("description"),
            args: args,
            requiredArgs: (object["required_args"]?.arrayValue ?? []).compactMap(\.stringValue),
            capabilities: (object["capabilities"]?.arrayValue ?? []).compactMap(\.stringValue),
            risk: risk,
            level: object["level"]?.stringValue ?? "L1")
    }
}

// MARK: - §2 handshake

public extension VoxProtocol {
    /// Il primo messaggio del client. Costruirlo è tutto ciò che serve per
    /// sapere cosa questo dispositivo annuncia — e quindi cosa il modello vedrà.
    static func hello(clientToken: String, clientID: String, platform: String,
                      appVersion: String, specs: [SkillSpec], capabilities: [String],
                      sampleRate: Int = 16000, channels: Int = 1,
                      audioFormat: String = "pcm_s16le") throws -> JSONValue {
        let unknown = capabilities.filter { !clientCapabilities.contains($0) }
        guard unknown.isEmpty else {
            throw ProtocolError("capability del client fuori dal vocabolario: \(unknown.sorted())")
        }
        guard !clientToken.isEmpty else {
            throw ProtocolError("client_token assente", code: "unauthorized")
        }
        return .object(["hello": .object([
            "protocol": .string(version),
            "client_token": .string(clientToken),
            "client_id": .string(clientID),
            "platform": .string(platform),
            "app_version": .string(appVersion),
            "audio": .object(["sample_rate": .int(sampleRate), "channels": .int(channels),
                              "format": .string(audioFormat)]),
            "capabilities": .array(capabilities.map { .string($0) }),
            "skills": .array(try specs.map { try wire($0) }),
        ])])
    }
}

/// La risposta del server. `acceptedSkills` è l'allowlist che vale per il resto
/// della sessione: ciò che non è qui non si esegue, qualunque cosa arrivi dopo.
public struct Welcome: Sendable, Equatable {
    public let session: String
    public let serverVersion: String
    public let acceptedSkills: Set<String>
    public let rejectedSkills: [[String: String]]
    public let limits: [String: JSONValue]
}

public extension VoxProtocol {
    static func welcome(from payload: JSONValue) throws -> Welcome {
        guard let object = payload.objectValue else {
            throw ProtocolError("welcome: atteso un oggetto JSON")
        }
        guard let session = object["session"]?.stringValue, !session.isEmpty else {
            throw ProtocolError("welcome.session assente")
        }
        guard let acceptedRaw = object["accepted_skills"]?.arrayValue else {
            throw ProtocolError("welcome.accepted_skills: attesa una lista di nomi")
        }
        let accepted = acceptedRaw.compactMap(\.stringValue)
        guard accepted.count == acceptedRaw.count else {
            throw ProtocolError("welcome.accepted_skills: attesa una lista di nomi")
        }
        var rejectedRaw: [JSONValue] = []
        if let raw = object["rejected_skills"] {
            guard let value = raw.arrayValue else {
                throw ProtocolError("welcome.rejected_skills: attesa una lista")
            }
            rejectedRaw = value
        }
        var limits: [String: JSONValue] = [:]
        if let raw = object["limits"] {
            guard let value = raw.objectValue else {
                throw ProtocolError("welcome.limits: atteso un oggetto JSON")
            }
            limits = value
        }
        return Welcome(
            session: session,
            serverVersion: object["server_version"]?.stringValue ?? "",
            acceptedSkills: Set(accepted),
            rejectedSkills: rejectedRaw.compactMap { entry in
                entry.objectValue?.compactMapValues(\.stringValue)
            },
            limits: limits)
    }
}

// MARK: - §5 esecuzione

/// La proposta del server. È una PROPOSTA: policy e conferma stanno sul
/// dispositivo che agisce (§0.1). `readback` arriva ma non fa testo: il client
/// la ricalcola in locale e vince la sua (§5.3).
public struct ExecuteRequest: Sendable, Equatable {
    public let callID: String
    public let name: String
    public let args: [String: JSONValue]
    public let session: String
    public let readback: String
    public let timeout: TimeInterval

    public init(callID: String, name: String, args: [String: JSONValue] = [:],
                session: String = "", readback: String = "", timeout: TimeInterval = 30) {
        self.callID = callID
        self.name = name
        self.args = args
        self.session = session
        self.readback = readback
        self.timeout = timeout
    }
}

/// Esattamente una risposta per `callID`.
public struct ExecuteResult: Sendable, Equatable {
    public let callID: String
    public let ok: Bool
    public let outcome: String
    public let speech: String
    public let data: JSONValue?
    public let synthesize: Bool
    public let tainted: Bool
    public let error: String?
    public let latencyMS: Int

    public init(callID: String, ok: Bool, outcome: String, speech: String = "",
                data: JSONValue? = nil, synthesize: Bool = false, tainted: Bool = false,
                error: String? = nil, latencyMS: Int = 0) throws {
        guard VoxProtocol.outcomes.contains(outcome) else {
            throw ProtocolError("outcome fuori dal vocabolario: '\(outcome)'")
        }
        self.callID = callID
        self.ok = ok
        self.outcome = outcome
        self.speech = speech
        self.data = data
        self.synthesize = synthesize
        self.tainted = tainted
        self.error = error
        self.latencyMS = latencyMS
    }

    public func toWire() -> JSONValue {
        var payloadData = data ?? .null
        var payloadError = error
        // §8: si tronca lato client. Mandare 300 KB e sperare che il server li
        // rifiuti bene è far decidere all'altro un limite che è nostro.
        if data != nil, VoxProtocol.byteCount(of: payloadData) > VoxProtocol.maxDataBytes {
            payloadData = .null
            payloadError = VoxProtocol.dataTooLarge
        }
        return .object(["result": .object([
            "call_id": .string(callID),
            "ok": .bool(ok),
            "speech": .string(VoxProtocol.clip(speech, VoxProtocol.maxTextBytes)),
            "data": payloadData,
            "synthesize": .bool(synthesize),
            "tainted": .bool(tainted),
            "outcome": .string(outcome),
            "error": payloadError.map { .string(VoxProtocol.clip($0, VoxProtocol.maxTextBytes)) } ?? .null,
            "latency_ms": .int(latencyMS),
        ])])
    }
}

public extension VoxProtocol {
    static func execute(from payload: JSONValue) throws -> ExecuteRequest {
        guard let object = payload.objectValue else {
            throw ProtocolError("execute: atteso un oggetto JSON")
        }
        guard let callID = object["call_id"]?.stringValue, !callID.isEmpty else {
            throw ProtocolError("execute.call_id assente")
        }
        guard let name = object["name"]?.stringValue, !name.isEmpty else {
            throw ProtocolError("execute.name assente", ref: callID)
        }
        var args: [String: JSONValue] = [:]
        if let raw = object["args"] {
            guard let value = raw.objectValue else {
                throw ProtocolError("execute.args: atteso un oggetto JSON", ref: callID)
            }
            args = value
        }
        var timeout: TimeInterval = 30
        if let raw = object["timeout_s"] {
            switch raw {
            case .int(let value) where value > 0: timeout = TimeInterval(value)
            case .double(let value) where value > 0: timeout = value
            default: throw ProtocolError("execute.timeout_s: atteso un numero positivo", ref: callID)
            }
        }
        var readback = ""
        if let raw = object["readback"], raw != .null {
            guard let value = raw.stringValue else {
                throw ProtocolError("execute.readback: atteso testo", ref: callID)
            }
            readback = value
        }
        return ExecuteRequest(callID: callID, name: name, args: args,
                              session: object["session"]?.stringValue ?? "",
                              readback: readback, timeout: timeout)
    }

    /// §8. Nessun messaggio riporta mai stack trace, percorsi del server o
    /// token: chi costruisce il testo lo sa, questa funzione non lo indovina.
    static func errorMessage(_ code: String, text: String = "", ref: String? = nil) throws -> JSONValue {
        guard errorCodes.contains(code) else {
            throw ProtocolError("codice di errore fuori dal vocabolario: '\(code)'")
        }
        var payload: [String: JSONValue] = ["code": .string(code)]
        if !text.isEmpty { payload["text"] = .string(clip(text, maxTextBytes)) }
        if let ref { payload["ref"] = .string(ref) }
        return .object(["error": .object(payload)])
    }
}

// MARK: - buste

public extension VoxProtocol {
    /// Un messaggio del server → `(kind, payload)`, con `kind` nell'allowlist.
    ///
    /// Le buste con più di una chiave, con una chiave sconosciuta o oltre i
    /// 64 KiB di §8 non sono messaggi ambigui da interpretare al meglio: sono
    /// `malformed`.
    static func decode(_ data: Data) throws -> (kind: String, payload: JSONValue) {
        guard data.count <= maxJSONBytes else {
            throw ProtocolError("messaggio oltre \(maxJSONBytes) byte")
        }
        guard let message = try? JSONDecoder().decode(JSONValue.self, from: data) else {
            throw ProtocolError("JSON non valido")
        }
        guard let object = message.objectValue, object.count == 1, let kind = object.keys.first else {
            throw ProtocolError("atteso un oggetto JSON con esattamente una chiave")
        }
        guard serverMessages.contains(kind) else {
            throw ProtocolError("messaggio sconosciuto: '\(kind)'")
        }
        return (kind, object[kind]!)
    }

    static func decode(_ text: String) throws -> (kind: String, payload: JSONValue) {
        try decode(Data(text.utf8))
    }

    /// Busta → testo da spedire. Chiavi ordinate: due esecuzioni devono
    /// produrre lo stesso byte, altrimenti un test sul filo non è confrontabile.
    static func encode(_ message: JSONValue) throws -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        let data = try encoder.encode(message)
        guard data.count <= maxJSONBytes else {
            throw ProtocolError("messaggio oltre \(maxJSONBytes) byte")
        }
        return String(decoding: data, as: UTF8.self)
    }

    // §4.1 — messaggi di controllo verso il server.
    static func ptt(down: Bool) -> JSONValue { .object(["ptt": .string(down ? "down" : "up")]) }
    static func stop() -> JSONValue { .object(["stop": .bool(true)]) }
    static func textInput(_ text: String) -> JSONValue {
        .object(["text": .string(clip(text, maxTextBytes))])
    }
    static func bye() -> JSONValue { .object(["bye": .bool(true)]) }

    // ------------------------------------------------------------------ utilità

    static func byteCount(of value: JSONValue) -> Int {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        guard let data = try? encoder.encode(value) else {
            // Non serializzabile: per il limite conta come «oltre», così il
            // troncamento di §8 scatta invece di far esplodere l'invio.
            return maxDataBytes + 1
        }
        return data.count
    }

    /// Taglia a `limit` byte UTF-8 senza spezzare un carattere a metà.
    static func clip(_ text: String, _ limit: Int) -> String {
        var encoded = Array(text.utf8)
        guard encoded.count > limit else { return text }
        encoded = Array(encoded.prefix(limit))
        while !encoded.isEmpty {
            if let value = String(bytes: encoded, encoding: .utf8) { return value }
            encoded.removeLast()
        }
        return ""
    }
}
