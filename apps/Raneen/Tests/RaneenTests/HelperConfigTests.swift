import Foundation
import XCTest

@testable import Raneen

/// The translation from settings to a command line.
///
/// This is the whole configuration mechanism: settings are stored by the
/// shell and applied by relaunching the core with new argv. So a mistake
/// here is not a cosmetic one — it is the core running with something other
/// than what the window says, which is the hardest class of bug to see.
final class HelperConfigTests: XCTestCase {

    private let socket = "/tmp/raneen-test.sock"

    private func argv(_ config: HelperConfig) -> [String] {
        config.argv(audioSocket: socket)
    }

    /// The value after a flag, so tests read as assertions about meaning
    /// rather than about array indices.
    private func value(of flag: String, in args: [String]) -> String? {
        guard let index = args.firstIndex(of: flag), index + 1 < args.count else { return nil }
        return args[index + 1]
    }

    // MARK: - Shape

    func testServeIsTheFirstArgument() {
        XCTAssertEqual(argv(HelperConfig()).first, "serve")
    }

    func testTheSocketIsAlwaysPassed() {
        XCTAssertEqual(value(of: "--audio-socket", in: argv(HelperConfig())), socket)
    }

    /// The shell owns the earcons (AD-16). Without this the core would beep
    /// into whatever output device was connected when it started.
    func testTheCoreIsAlwaysToldToStaySilent() {
        XCTAssertTrue(argv(HelperConfig()).contains("--no-sound"))
    }

    /// The core reads argument 0 as the model path, so it has to precede
    /// the flags — and a flag's *value* must never be mistaken for it.
    func testAModelPathIsThePositionalBeforeAnyFlag() {
        var config = HelperConfig()
        config.modelPath = "/models/ggml-small.en.bin"
        let args = argv(config)

        XCTAssertEqual(args[1], "/models/ggml-small.en.bin")
        XCTAssertLessThan(
            args.firstIndex(of: "/models/ggml-small.en.bin")!,
            args.firstIndex(of: "--audio-socket")!
        )
    }

    /// No model path means "whatever the core finds" — the bundled model,
    /// then the user cache. Resolving it here would be a second answer to a
    /// question the core already answers.
    func testNoModelPathLeavesTheChoiceToTheCore() {
        let args = argv(HelperConfig())
        XCTAssertEqual(args[1], "--audio-socket", "an empty model path became a positional")
    }

    // MARK: - Defaults

    /// Pins the defaults against `Policy::dictation` in the core. They are
    /// written in both places — `HelperConfig`'s property initialisers are
    /// the shell's only copy — so this is the tripwire if either side moves.
    func testDefaultsMirrorTheCore() {
        let args = argv(HelperConfig())
        XCTAssertEqual(value(of: "--trigger", in: args), "hold")
        XCTAssertEqual(value(of: "--vad", in: args), "silero")
        XCTAssertEqual(value(of: "--language", in: args), "en")
        XCTAssertEqual(value(of: "--silence-frames", in: args), "8")
        XCTAssertEqual(value(of: "--pre-roll-frames", in: args), "3")
        XCTAssertEqual(value(of: "--max-seconds", in: args), "30")
        XCTAssertEqual(value(of: "--min-confidence", in: args), "0")
    }

    /// `--max-seconds 30.0` parses, but it reads like a mistake in a log
    /// line and invites the question of whether the flag wants a float.
    func testWholeNumbersAreNotWrittenWithADecimalPoint() {
        var config = HelperConfig()
        config.maxSeconds = 45
        config.minConfidence = 0.35
        let args = argv(config)
        XCTAssertEqual(value(of: "--max-seconds", in: args), "45")
        XCTAssertEqual(value(of: "--min-confidence", in: args), "0.35")
    }

    // MARK: - Timings that follow the trigger

    /// A wake word must tolerate a thinking pause. 640 ms is an ordinary one
    /// while composing a request out loud, so the dictation threshold ends the
    /// turn halfway through and hands back half a sentence.
    func testAWakeWordWaitsLongerThanAHeldKey() {
        XCTAssertGreaterThan(
            HelperConfig.recommendedSilenceFrames(for: .wakeword),
            HelperConfig.recommendedSilenceFrames(for: .hold)
        )
        // At least 1.5 s of tolerance, in frames of 80 ms.
        XCTAssertGreaterThanOrEqual(HelperConfig.recommendedSilenceFrames(for: .wakeword), 19)
    }

    /// A key press is an exact instant; every other trigger is a detector that
    /// reports ~240 ms late, so it needs pre-roll or it clips its first word.
    func testEveryDetectorTriggerKeepsMorePreRollThanAKey() {
        for trigger in [TriggerMode.vad, .toggle, .wakeword] {
            XCTAssertGreaterThan(
                HelperConfig.recommendedPreRollFrames(for: trigger),
                HelperConfig.recommendedPreRollFrames(for: .hold),
                "\(trigger) opens on a detector and would clip the first word"
            )
        }
    }

    /// The regression this class of bug produces: argv always carries these
    /// flags, so the core's own mode-dependent defaults can never apply. A
    /// wake-word config must therefore *emit* the wake-word timings.
    func testWakeWordArgvCarriesTheWakeWordTimings() {
        var config = HelperConfig()
        config.trigger = .wakeword
        config.silenceFrames = HelperConfig.recommendedSilenceFrames(for: .wakeword)
        config.preRollFrames = HelperConfig.recommendedPreRollFrames(for: .wakeword)
        let args = argv(config)

        XCTAssertEqual(value(of: "--silence-frames", in: args), "25")
        XCTAssertEqual(value(of: "--pre-roll-frames", in: args), "10")
    }

    // MARK: - Engine selection

    func testLocalNamesTheEngineAndPassesNoURL() {
        var config = HelperConfig()
        config.engine = .local
        let args = argv(config)
        XCTAssertEqual(value(of: "--stt", in: args), "local")
        XCTAssertFalse(args.contains("--stt-url"))
    }

    /// The URL's scheme picks the engine in the core, so passing `--stt`
    /// alongside it creates a state where the two can disagree.
    func testAURLIsPassedWithoutAlsoNamingTheEngine() {
        var config = HelperConfig()
        config.engine = .remote
        config.remoteURL = "http://nas.local:8000/v1"
        config.remoteModel = "my-whisper"
        let args = argv(config)

        XCTAssertEqual(value(of: "--stt-url", in: args), "http://nas.local:8000/v1")
        XCTAssertEqual(value(of: "--stt-model", in: args), "my-whisper")
        XCTAssertFalse(args.contains("--stt"), "the URL and --stt could disagree")
    }

    /// Choosing an engine without giving a URL is how you reach OpenAI's
    /// own default endpoint, so it must still name the engine.
    func testChoosingRemoteWithNoURLStillNamesTheEngine() {
        var config = HelperConfig()
        config.engine = .realtime
        let args = argv(config)
        XCTAssertEqual(value(of: "--stt", in: args), "realtime")
    }

    /// On by default, and only mentioned when turned off — a bundled model
    /// means a dead network costs accuracy rather than the sentence.
    func testFallbackIsOnlyDisabledExplicitly() {
        var config = HelperConfig()
        config.engine = .remote
        config.remoteURL = "http://nas.local:8000/v1"
        XCTAssertFalse(argv(config).contains("--stt-fallback"))

        config.remoteFallback = false
        XCTAssertEqual(value(of: "--stt-fallback", in: argv(config)), "none")
    }

    // MARK: - Wake word

    func testEveryWakeWordGetsItsOwnFlag() {
        var config = HelperConfig()
        config.wakeWords = ["/w/alexa.onnx", "/w/hey_jarvis.onnx"]
        let args = argv(config)

        XCTAssertEqual(args.filter { $0 == "--wake-word" }.count, 2)
        XCTAssertTrue(args.contains("/w/alexa.onnx"))
        XCTAssertTrue(args.contains("/w/hey_jarvis.onnx"))
    }

    /// Tuning flags for a detector that is not armed would be noise in the
    /// command line, and imply something is listening when nothing is.
    func testWakeTuningIsOmittedWhenNoWordIsArmed() {
        let args = argv(HelperConfig())
        XCTAssertFalse(args.contains("--wake-threshold"))
        XCTAssertFalse(args.contains("--wake-patience"))
        XCTAssertFalse(args.contains("--wake-cooldown"))
    }

    func testWakeTuningTravelsWithTheWords() {
        var config = HelperConfig()
        config.wakeWords = ["/w/alexa.onnx"]
        config.wakeThreshold = 0.7
        config.wakePatience = 2
        config.wakeCooldownFrames = 30
        let args = argv(config)

        XCTAssertEqual(value(of: "--wake-threshold", in: args), "0.7")
        XCTAssertEqual(value(of: "--wake-patience", in: args), "2")
        XCTAssertEqual(value(of: "--wake-cooldown", in: args), "30")
    }

    /// A wake word is reported in every trigger mode and obeyed in one.
    /// Arming a detector must not silently take push-to-talk away, so the
    /// trigger stays exactly what was asked for.
    func testArmingAWakeWordDoesNotChangeTheTrigger() {
        var config = HelperConfig()
        config.trigger = .hold
        config.wakeWords = ["/w/alexa.onnx"]
        XCTAssertEqual(value(of: "--trigger", in: argv(config)), "hold")
    }

    // MARK: - Broadcast

    func testBroadcastIsOffByDefaultAndPublishesNothing() {
        XCTAssertEqual(HelperConfig().broadcast, .off)
        XCTAssertFalse(argv(HelperConfig()).contains("--zmq-pub"))
    }

    /// The distinction a checkbox would hide: loopback is reachable by this
    /// Mac, `*` is reachable by the network.
    func testLoopbackAndNetworkBindDifferentAddresses() {
        var config = HelperConfig()
        config.broadcastPort = 5599

        config.broadcast = .loopback
        XCTAssertEqual(value(of: "--zmq-pub", in: argv(config)), "tcp://127.0.0.1:5599")

        config.broadcast = .network
        XCTAssertEqual(value(of: "--zmq-pub", in: argv(config)), "tcp://*:5599")
    }

    // MARK: - Secrets

    /// `ps` shows full command lines to every process on the machine. A key
    /// in argv is a key published to anything running as the user, so this
    /// asserts the absence rather than trusting review to catch it.
    func testNoSecretIsEverPassedOnTheCommandLine() {
        var config = HelperConfig()
        config.engine = .remote
        config.remoteURL = "https://api.openai.com/v1"
        config.wakeWords = ["/w/alexa.onnx"]
        config.broadcast = .network

        let args = argv(config)
        XCTAssertFalse(args.contains("--stt-key"))
        XCTAssertFalse(args.joined(separator: " ").lowercased().contains("sk-"))
    }

    // MARK: - Model library

    /// The language choice is not independent of the model choice: a `.en`
    /// model given other speech returns confident nonsense rather than
    /// failing, so the UI has to know which models are English-only.
    func testEnglishOnlyModelsAreRecognisedByName() {
        XCTAssertTrue(WhisperModel(path: "/m/ggml-base.en-q5_1.bin").isEnglishOnly)
        XCTAssertTrue(WhisperModel(path: "/m/ggml-small.en.bin").isEnglishOnly)
        XCTAssertFalse(WhisperModel(path: "/m/ggml-base.bin").isEnglishOnly)
        XCTAssertFalse(WhisperModel(path: "/m/ggml-large-v3.bin").isEnglishOnly)
    }

    func testAModelIsNamedByItsFileRatherThanItsPath() {
        XCTAssertEqual(
            WhisperModel(path: "/a/very/long/path/ggml-tiny.en.bin").name, "ggml-tiny.en.bin")
    }
}

/// Reading settings back out of `UserDefaults`.
final class SettingsStoreTests: XCTestCase {

    private var defaults: UserDefaults!

    override func setUp() {
        super.setUp()
        // An isolated suite: these tests must not read or write the real
        // app's settings, and they must not depend on what a previous run
        // happened to leave behind.
        //
        // **Nothing is registered or pre-seeded here, deliberately.** The
        // earlier version of this test called `registerDefaults` first, which
        // meant it tested the mechanism the app depended on instead of the
        // situation the app was actually in — a completely empty defaults
        // database. It passed while the shipped app launched the core with
        // `--max-seconds 0` and transcribed nothing.
        defaults = UserDefaults(suiteName: "raneen.tests.\(UUID().uuidString)")
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: defaults.description)
        defaults = nil
        super.tearDown()
    }

    /// A fresh install must behave exactly as the core does on its own —
    /// **from a genuinely empty defaults database**, with nothing registered.
    ///
    /// This is the regression test for the bug that shipped: every numeric
    /// setting came back as 0, because `UserDefaults.integer(forKey:)` answers
    /// 0 rather than nil for a key it has never seen.
    func testAFreshInstallReadsBackTheDefaults() {
        XCTAssertEqual(SettingsStore.current(defaults), HelperConfig())
    }

    /// The specific value that broke dictation, asserted on its own so a
    /// failure names the cause rather than reporting two unequal structs.
    func testNoNumericSettingCanEverBeZeroByAccident() {
        let config = SettingsStore.current(defaults)
        XCTAssertGreaterThan(
            config.maxSeconds, 0,
            "a zero segment limit force-cuts every turn and transcribes nothing")
        XCTAssertGreaterThan(config.silenceFrames, 0)
        XCTAssertGreaterThan(config.broadcastPort, 0)
        XCTAssertGreaterThan(config.wakeThreshold, 0)
        XCTAssertGreaterThan(config.wakePatience, 0)
        XCTAssertGreaterThan(config.wakeCooldownFrames, 0)
    }

    /// Existing installs already have the zeros on disk, so reading has to
    /// repair them rather than trust them.
    func testStoredNonsenseIsReplacedByTheDefault() {
        defaults.set(0, forKey: SettingsStore.Key.silenceFrames)
        defaults.set(0.0, forKey: SettingsStore.Key.maxSeconds)
        defaults.set(0, forKey: SettingsStore.Key.broadcastPort)
        defaults.set(99, forKey: SettingsStore.Key.wakePatience)

        let config = SettingsStore.current(defaults)
        XCTAssertEqual(config.silenceFrames, HelperConfig().silenceFrames)
        XCTAssertEqual(config.maxSeconds, HelperConfig().maxSeconds)
        XCTAssertEqual(config.broadcastPort, HelperConfig().broadcastPort)
        XCTAssertEqual(config.wakePatience, HelperConfig().wakePatience)
    }

    /// But a stored zero that *means* something must survive. Pre-roll of 0
    /// frames is a legitimate choice, which is why presence is tested with
    /// `object(forKey:)` rather than by comparing against zero.
    func testAMeaningfulZeroIsKept() {
        defaults.set(0, forKey: SettingsStore.Key.preRollFrames)
        XCTAssertEqual(SettingsStore.current(defaults).preRollFrames, 0)
    }

    /// Saving and reading back must be lossless, or an Apply would quietly
    /// change something the user did not touch.
    func testEverySettingSurvivesSaveAndReload() {
        var config = HelperConfig()
        config.trigger = .toggle
        config.engine = .remote
        config.modelPath = "/models/ggml-small.bin"
        config.language = "hi"
        config.remoteURL = "http://nas.local:8000/v1"
        config.remoteModel = "faster-whisper-large"
        config.remoteFallback = false
        config.vad = .energy
        config.silenceFrames = 16
        config.preRollFrames = 6
        config.maxSeconds = 60
        config.minConfidence = 0.4
        config.wakeWords = ["/w/alexa.onnx", "/w/hey_jarvis.onnx"]
        config.wakeThreshold = 0.8
        config.wakePatience = 3
        config.wakeCooldownFrames = 40
        config.broadcast = .network
        config.broadcastPort = 5600

        SettingsStore.save(config, to: defaults)
        XCTAssertEqual(SettingsStore.current(defaults), config)
    }

    func testStoredChoicesSurviveTheRoundTrip() {
        defaults.set("wakeword", forKey: SettingsStore.Key.trigger)
        defaults.set("energy", forKey: SettingsStore.Key.vad)
        defaults.set("network", forKey: SettingsStore.Key.broadcast)
        defaults.set(20, forKey: SettingsStore.Key.silenceFrames)
        defaults.set(0.65, forKey: SettingsStore.Key.wakeThreshold)
        defaults.set(["/w/alexa.onnx"], forKey: SettingsStore.Key.wakeWords)

        let config = SettingsStore.current(defaults)
        XCTAssertEqual(config.trigger, .wakeword)
        XCTAssertEqual(config.vad, .energy)
        XCTAssertEqual(config.broadcast, .network)
        XCTAssertEqual(config.silenceFrames, 20)
        XCTAssertEqual(config.wakeThreshold, 0.65)
        XCTAssertEqual(config.wakeWords, ["/w/alexa.onnx"])
    }

    /// A value that is no longer one of the choices — an older build's
    /// setting, or a hand-edited plist — must not stop the app launching.
    func testAnUnrecognisedStoredValueFallsBackToTheDefault() {
        defaults.set("telepathy", forKey: SettingsStore.Key.trigger)
        XCTAssertEqual(SettingsStore.current(defaults).trigger, .hold)
    }

    /// An empty model path means "the bundled model", and must not become a
    /// positional argument of `""` — which the core would try to load.
    func testAnEmptyModelPathIsTreatedAsUnset() {
        defaults.set("", forKey: SettingsStore.Key.modelPath)
        XCTAssertNil(SettingsStore.current(defaults).modelPath)
    }
}

/// The rules the settings window applies while editing.
final class SettingsModelTests: XCTestCase {

    /// Switching trigger must carry the timings with it. Otherwise choosing
    /// "when I say a wake word" quietly applies push-to-talk timings, and the
    /// turn ends on the first thinking pause.
    func testChangingTriggerMovesUntouchedTimings() {
        let model = SettingsModel()
        model.config.trigger = .hold
        model.useRecommendedTimings()

        model.config.trigger = .wakeword

        XCTAssertEqual(
            model.config.silenceFrames, HelperConfig.recommendedSilenceFrames(for: .wakeword))
        XCTAssertEqual(
            model.config.preRollFrames, HelperConfig.recommendedPreRollFrames(for: .wakeword))
        XCTAssertTrue(model.timingsAreRecommended)
    }

    /// But a deliberate choice must survive a mode switch — otherwise the
    /// window silently discards what the user set.
    func testChangingTriggerLeavesACustomisedTimingAlone() {
        let model = SettingsModel()
        model.config.trigger = .hold
        model.config.silenceFrames = 40

        model.config.trigger = .wakeword

        XCTAssertEqual(model.config.silenceFrames, 40, "a deliberate value was overwritten")
        XCTAssertFalse(model.timingsAreRecommended)
    }

    /// And there has to be a way back, or one experiment with the stepper opts
    /// you out of the recommendations for good.
    func testRecommendedTimingsCanBeRestored() {
        let model = SettingsModel()
        model.config.trigger = .wakeword
        model.config.silenceFrames = 3
        XCTAssertFalse(model.timingsAreRecommended)

        model.useRecommendedTimings()
        XCTAssertTrue(model.timingsAreRecommended)
    }
}
