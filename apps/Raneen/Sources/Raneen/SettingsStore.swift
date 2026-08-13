import Foundation

/// `UserDefaults` keys, and the bridge between stored settings and the
/// `HelperConfig` the core is launched with.
///
/// Kept apart from `HelperConfig` so that type stays pure: building a
/// command line is testable without a defaults database, and the mapping
/// is testable without a running app.
///
/// The keys are namespaced because `UserDefaults.standard` for an app is
/// one flat dictionary shared with AppKit's own state, and an unprefixed
/// `language` or `trigger` is asking for a collision.
enum SettingsStore {

    enum Key {
        static let trigger = "raneen.trigger"
        static let engine = "raneen.stt.engine"
        static let modelPath = "raneen.stt.modelPath"
        static let language = "raneen.stt.language"
        static let remoteURL = "raneen.stt.remoteURL"
        static let remoteModel = "raneen.stt.remoteModel"
        static let remoteFallback = "raneen.stt.remoteFallback"
        static let vad = "raneen.vad.kind"
        static let silenceFrames = "raneen.vad.silenceFrames"
        static let preRollFrames = "raneen.vad.preRollFrames"
        static let maxSeconds = "raneen.vad.maxSeconds"
        static let minConfidence = "raneen.vad.minConfidence"
        static let wakeWords = "raneen.wake.models"
        static let wakeThreshold = "raneen.wake.threshold"
        static let wakePatience = "raneen.wake.patience"
        static let wakeCooldown = "raneen.wake.cooldownFrames"
        static let broadcast = "raneen.broadcast.mode"
        static let broadcastPort = "raneen.broadcast.port"
    }

    /// Valid ranges, and the only place they are written.
    ///
    /// These exist because a number outside them is not a preference, it is
    /// a corrupt one — and the core cannot always tell. `--max-seconds 0`
    /// force-cuts every segment the instant it opens, so audio flows, the
    /// level meter animates, and no transcript is ever produced. That
    /// shipped once, and it presented as "dictation is broken" rather than
    /// as a bad setting.
    enum Limits {
        static let silenceFrames = 2...100
        static let preRollFrames = 0...50
        static let maxSeconds = 1.0...300.0
        static let minConfidence = 0.0...0.95
        static let wakeThreshold = 0.01...0.99
        static let wakePatience = 1...20
        static let wakeCooldown = 1...200
        static let broadcastPort = 1024...65535
    }

    /// What the core should be launched with right now.
    ///
    /// **Every read falls back to `HelperConfig`'s own default**, and does so
    /// without depending on `register(defaults:)` having run. That
    /// registration used to be the mechanism, and it was fragile in exactly
    /// the way that matters: `UserDefaults.integer(forKey:)` returns `0` for
    /// an unregistered key rather than nil, `SettingsModel()` is a stored
    /// property so it was constructed *before*
    /// `applicationDidFinishLaunching` could register anything, and the zeros
    /// it read were then saved back. One ordering mistake became a permanent
    /// broken configuration on disk.
    ///
    /// So the struct is the single source of defaults, presence is tested
    /// explicitly, and anything out of range is treated as absent.
    static func current(_ defaults: UserDefaults = .standard) -> HelperConfig {
        var config = HelperConfig()

        if let raw = defaults.string(forKey: Key.trigger), let value = TriggerMode(rawValue: raw) {
            config.trigger = value
        }
        if let raw = defaults.string(forKey: Key.engine), let value = SttEngine(rawValue: raw) {
            config.engine = value
        }
        if let raw = defaults.string(forKey: Key.vad), let value = VadKind(rawValue: raw) {
            config.vad = value
        }
        if let raw = defaults.string(forKey: Key.broadcast),
            let value = BroadcastMode(rawValue: raw)
        {
            config.broadcast = value
        }

        // Empty string means "the bundled model", not a path of "".
        if let path = defaults.string(forKey: Key.modelPath), !path.isEmpty {
            config.modelPath = path
        }
        if let language = defaults.string(forKey: Key.language), !language.isEmpty {
            config.language = language
        }
        config.remoteURL = defaults.string(forKey: Key.remoteURL) ?? config.remoteURL
        config.remoteModel = defaults.string(forKey: Key.remoteModel) ?? config.remoteModel
        if defaults.object(forKey: Key.remoteFallback) != nil {
            config.remoteFallback = defaults.bool(forKey: Key.remoteFallback)
        }
        config.wakeWords = defaults.stringArray(forKey: Key.wakeWords) ?? []

        config.silenceFrames = int(
            defaults, Key.silenceFrames, in: Limits.silenceFrames, or: config.silenceFrames)
        config.preRollFrames = int(
            defaults, Key.preRollFrames, in: Limits.preRollFrames, or: config.preRollFrames)
        config.maxSeconds = double(
            defaults, Key.maxSeconds, in: Limits.maxSeconds, or: config.maxSeconds)
        config.minConfidence = double(
            defaults, Key.minConfidence, in: Limits.minConfidence, or: config.minConfidence)
        config.wakeThreshold = double(
            defaults, Key.wakeThreshold, in: Limits.wakeThreshold, or: config.wakeThreshold)
        config.wakePatience = int(
            defaults, Key.wakePatience, in: Limits.wakePatience, or: config.wakePatience)
        config.wakeCooldownFrames = int(
            defaults, Key.wakeCooldown, in: Limits.wakeCooldown, or: config.wakeCooldownFrames)
        config.broadcastPort = int(
            defaults, Key.broadcastPort, in: Limits.broadcastPort, or: config.broadcastPort)

        return config
    }

    /// A stored integer, or the fallback when it is missing or nonsense.
    ///
    /// Presence is tested with `object(forKey:)` rather than by comparing to
    /// zero, because zero is a legitimate value for some of these — pre-roll
    /// of 0 frames means "no pre-roll", which someone may genuinely want.
    private static func int(
        _ defaults: UserDefaults, _ key: String, in range: ClosedRange<Int>, or fallback: Int
    ) -> Int {
        guard defaults.object(forKey: key) != nil else { return fallback }
        let stored = defaults.integer(forKey: key)
        guard range.contains(stored) else {
            // Repairs a plist already written by the version that saved
            // zeros, as well as anything hand-edited.
            Log.app.error("\(key) is \(stored), outside \(range) — using \(fallback)")
            return fallback
        }
        return stored
    }

    private static func double(
        _ defaults: UserDefaults, _ key: String, in range: ClosedRange<Double>, or fallback: Double
    ) -> Double {
        guard defaults.object(forKey: key) != nil else { return fallback }
        let stored = defaults.double(forKey: key)
        guard range.contains(stored) else {
            Log.app.error("\(key) is \(stored), outside \(range) — using \(fallback)")
            return fallback
        }
        return stored
    }

    /// Persist a configuration. Writing the whole struct rather than
    /// individual keys keeps the stored shape and `HelperConfig` from
    /// drifting apart one forgotten field at a time.
    static func save(_ config: HelperConfig, to defaults: UserDefaults = .standard) {
        defaults.set(config.trigger.rawValue, forKey: Key.trigger)
        defaults.set(config.engine.rawValue, forKey: Key.engine)
        defaults.set(config.modelPath ?? "", forKey: Key.modelPath)
        defaults.set(config.language, forKey: Key.language)
        defaults.set(config.remoteURL, forKey: Key.remoteURL)
        defaults.set(config.remoteModel, forKey: Key.remoteModel)
        defaults.set(config.remoteFallback, forKey: Key.remoteFallback)
        defaults.set(config.vad.rawValue, forKey: Key.vad)
        defaults.set(config.silenceFrames, forKey: Key.silenceFrames)
        defaults.set(config.preRollFrames, forKey: Key.preRollFrames)
        defaults.set(config.maxSeconds, forKey: Key.maxSeconds)
        defaults.set(config.minConfidence, forKey: Key.minConfidence)
        defaults.set(config.wakeWords, forKey: Key.wakeWords)
        defaults.set(config.wakeThreshold, forKey: Key.wakeThreshold)
        defaults.set(config.wakePatience, forKey: Key.wakePatience)
        defaults.set(config.wakeCooldownFrames, forKey: Key.wakeCooldown)
        defaults.set(config.broadcast.rawValue, forKey: Key.broadcast)
        defaults.set(config.broadcastPort, forKey: Key.broadcastPort)
    }
}

// MARK: - What is on disk

/// A whisper model the user can choose between.
struct WhisperModel: Identifiable, Hashable {
    let path: String
    var id: String { path }

    var name: String { (path as NSString).lastPathComponent }

    /// Whether this model can only produce English.
    ///
    /// An `.en` model given other speech does not fail — it transliterates
    /// into English phonemes and returns confident nonsense that reads like
    /// a hallucination. So the language choice is not independent of the
    /// model choice, and a UI that let them disagree would be promising
    /// something impossible.
    var isEnglishOnly: Bool { name.contains(".en") }

    var sizeDescription: String {
        let attributes = try? FileManager.default.attributesOfItem(atPath: path)
        guard let bytes = attributes?[.size] as? Int64 else { return "" }
        return ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }
}

enum ModelLibrary {

    /// The writable half of the search path, and where downloads go.
    ///
    /// The other candidate is inside the app bundle, which is read-only and
    /// signed — so this is not merely a preference about tidiness, it is the
    /// only one of the two a download could use.
    ///
    /// `RANEEN_MODEL_DIR` overrides it, matching `RANEEN_WAKEWORD_DIR` next
    /// door. Two reasons beyond symmetry: `large-v3` is 3.1 GB and a boot
    /// disk is not always where that should live, and without an override
    /// every test of the download machinery would have to write into the
    /// real cache directory.
    static var userDirectory: URL {
        if let override = ProcessInfo.processInfo.environment["RANEEN_MODEL_DIR"],
            !override.isEmpty
        {
            return URL(fileURLWithPath: override)
        }
        return URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent(".cache/raneen/models")
    }

    /// Where the core looks, in the order it looks: beside the executable
    /// (inside the bundle that is `Contents/Resources/helper`), then the
    /// user cache. Mirrored rather than asked for, because the core has no
    /// "list models" mode — and if it grows one, this should call it.
    static var searchPaths: [URL] {
        var paths: [URL] = []
        if let helper = Bundle.main.resourceURL?.appendingPathComponent("helper") {
            paths.append(helper)
        }
        paths.append(userDirectory)
        return paths
    }

    /// Every ggml model on disk, deduplicated by filename with the earlier
    /// search path winning — the same precedence the core applies.
    static func available() -> [WhisperModel] {
        var seen = Set<String>()
        var models: [WhisperModel] = []
        for directory in searchPaths {
            let contents =
                (try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? []
            for name in contents.sorted() where name.hasSuffix(".bin") {
                guard !seen.contains(name) else { continue }
                seen.insert(name)
                models.append(
                    WhisperModel(path: directory.appendingPathComponent(name).path))
            }
        }
        return models
    }
}

enum WakeWordLibrary {

    /// The two models every openWakeWord classifier shares. Without them
    /// no wake word can run at all, whatever classifiers are configured.
    static let featureModels = ["melspectrogram.onnx", "embedding_model.onnx"]

    /// Where the core looks for the shared feature models, in its order.
    static var searchPaths: [URL] {
        var paths: [URL] = []
        if let override = ProcessInfo.processInfo.environment["RANEEN_WAKEWORD_DIR"] {
            paths.append(URL(fileURLWithPath: override))
        }
        if let helper = Bundle.main.resourceURL?.appendingPathComponent("helper") {
            paths.append(helper)
        }
        paths.append(
            URL(fileURLWithPath: NSHomeDirectory())
                .appendingPathComponent(".cache/raneen/wakeword")
        )
        return paths
    }

    /// Whether the shared feature models are present.
    ///
    /// Checked in the UI rather than left to fail at launch: they are not
    /// shipped in the bundle, so "wake word configured but unavailable" is
    /// an ordinary state a new user will be in, not an error. Presenting it
    /// as one — a core that exits at startup — would look like a bug.
    static func featureModelsAvailable() -> Bool {
        searchPaths.contains { directory in
            featureModels.allSatisfy {
                FileManager.default.fileExists(atPath: directory.appendingPathComponent($0).path)
            }
        }
    }
}
