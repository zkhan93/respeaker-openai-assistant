import Foundation

/// What the settings window edits, and what the app is actually running.
///
/// The two are separate on purpose. Configuration is applied by relaunching
/// the core with new argv, so between an edit and an Apply the stored
/// settings and the live process genuinely disagree — and a window that
/// hides that would claim a change had taken effect when it had not.
/// `isDirty` is what the Apply button reads.
final class SettingsModel: ObservableObject {

    /// Edited by the window, persisted on every change. Persisting eagerly
    /// rather than on Apply means a change survives a crash or a quit; it is
    /// only the *running core* that waits for Apply.
    @Published var config: HelperConfig {
        didSet { normaliseAndSave(previous: oldValue) }
    }

    /// What the running core was launched with, or `nil` before the first
    /// spawn. Set by `AppDelegate` at spawn time, which is the only place
    /// that knows for certain.
    @Published var running: HelperConfig?

    /// The listening animation.
    ///
    /// **Separate from `config`, and deliberately not part of `isDirty`.**
    /// Nothing about it reaches the core, so it takes effect the instant it
    /// is chosen — folding it into `HelperConfig` would have lit "the core
    /// is still running the previous settings" for a change the core never
    /// sees, and offered a restart that would do nothing.
    @Published var indicatorStyle: IndicatorStyle {
        didSet {
            guard indicatorStyle != oldValue else { return }
            IndicatorPreference.save(indicatorStyle, to: defaults)
            onIndicatorStyleChange?(indicatorStyle)
        }
    }

    /// Applying the style to the live panel. Owned by `AppDelegate` for the
    /// same reason `onApply` is: this type does not know about windows.
    var onIndicatorStyleChange: ((IndicatorStyle) -> Void)?

    /// Whether the shared openWakeWord feature models are on disk. Cached
    /// rather than checked in `body`: SwiftUI re-evaluates a view many times
    /// and hitting the filesystem on each pass is needless.
    @Published private(set) var wakeFeatureModelsAvailable: Bool

    /// Models found on disk, for the picker.
    @Published private(set) var models: [WhisperModel]

    /// Voices the core has learned, newest last.
    ///
    /// **Owned by the core, mirrored here.** The store on disk is the
    /// truth; this is what the last `speakers` reply said. Editing a name
    /// sends a command and waits for the reply rather than mutating this
    /// directly — otherwise a rename the core rejected would still show as
    /// applied.
    @Published private(set) var speakers: [Helper.SpeakerProfile] = []

    /// Whether the voiceprint model is on this Mac. Cached for the same
    /// reason `wakeFeatureModelsAvailable` is: `body` runs often and this
    /// is a filesystem check.
    @Published private(set) var speakerModelAvailable: Bool = false

    /// Talking to the running core about speakers. Owned by `AppDelegate`,
    /// which is the only thing that holds the process.
    var onSpeakerCommand: ((SpeakerCommand) -> Void)?

    /// What the window can ask the core to do to the roster.
    enum SpeakerCommand {
        case list
        case name(id: String, name: String)
        case forget(id: String)
        /// Attach the next few seconds of speech to this name; an empty
        /// name cancels.
        case learn(name: String)
    }

    /// Who the core is currently waiting to hear, if anyone.
    ///
    /// Drives the "say something" state in the pane. Cleared when a
    /// `speaker_identified` arrives carrying that name, which is the
    /// core confirming it actually heard them — not when the command is
    /// sent, because that only means the request left.
    @Published private(set) var learning: String?

    /// Fetches models from the catalogue.
    ///
    /// Owned here rather than by `AppDelegate` because it is settings-window
    /// state and nothing else in the app needs it. Its `URLSession` is lazy,
    /// so constructing this at launch costs a directory scan and nothing
    /// more.
    let downloader = ModelDownloader()

    /// Applying means restarting the core. Owned by `AppDelegate` because
    /// process lifecycle is its job, not this type's.
    var onApply: (() -> Void)?

    /// Where settings are read from and written to.
    ///
    /// Injectable for tests only — the app always gets `.standard`. Without
    /// it a test constructing this type writes real `raneen.*` keys into
    /// whatever domain the test runner happens to own, and the next
    /// `SettingsModel()` in the same process reads them back: state leaking
    /// from one test into another through disk.
    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        config = SettingsStore.current(defaults)
        indicatorStyle = IndicatorPreference.current(defaults)
        models = ModelLibrary.available()
        wakeFeatureModelsAvailable = WakeWordLibrary.featureModelsAvailable()

        // **Nothing is written here.** `current()` already repairs what it
        // reads, and an earlier version called `normaliseAndSave()` at this
        // point — which persisted values nobody had chosen. Combined with
        // reading before defaults were registered, that turned a momentary
        // bad read into a permanently broken configuration on disk. Writing
        // settings is now something only an edit does.
        if selectedModelIsEnglishOnly { config.language = "en" }

        // Someone who downloads a model wants to use it. Making them then
        // find it in a picker is a step with no decision in it — and the
        // core is not restarted here, so this behaves like any other edit:
        // the footer lights up and Apply is theirs to press.
        downloader.onInstalled = { [weak self] _, url in
            guard let self else { return }
            self.config.modelPath = url.path
            self.refreshLibraries()
        }
    }

    /// Whether the timings are this trigger's recommended ones, so the window
    /// can say so rather than leaving two bare numbers to interpret.
    var timingsAreRecommended: Bool {
        config.silenceFrames == HelperConfig.recommendedSilenceFrames(for: config.trigger)
            && config.preRollFrames == HelperConfig.recommendedPreRollFrames(for: config.trigger)
    }

    /// Put the timings back to this trigger's recommendation.
    ///
    /// Needed because the follow-the-trigger rule deliberately leaves a value
    /// alone once it has been customised — which is right, but without a way
    /// back it means one experiment with the stepper opts you out of the
    /// recommendations permanently.
    func useRecommendedTimings() {
        var adjusted = config
        adjusted.silenceFrames = HelperConfig.recommendedSilenceFrames(for: config.trigger)
        adjusted.preRollFrames = HelperConfig.recommendedPreRollFrames(for: config.trigger)
        config = adjusted
    }

    /// True when the stored settings differ from what the core is running.
    ///
    /// `false` before the first spawn: with nothing running there is nothing
    /// to be out of step with, and an Apply button lit on launch would
    /// suggest the settings had not taken effect.
    var isDirty: Bool {
        guard let running else { return false }
        return running != config
    }

    func apply() {
        onApply?()
    }

    /// Ask the core for the roster. Cheap, and the reply refreshes
    /// `speakers`.
    func refreshSpeakers() {
        speakerModelAvailable = SettingsModel.speakerModelIsInstalled
        onSpeakerCommand?(.list)
    }

    func nameSpeaker(_ id: String, as name: String) {
        onSpeakerCommand?(.name(id: id, name: name))
    }

    func forgetSpeaker(_ id: String) {
        onSpeakerCommand?(.forget(id: id))
    }

    func learnSpeaker(named name: String) {
        learning = name
        onSpeakerCommand?(.learn(name: name))
    }

    func cancelLearning() {
        learning = nil
        onSpeakerCommand?(.learn(name: ""))
    }

    /// Called when the core reports a voice it just learned.
    func learned(_ name: String?) {
        guard let name, learning == name else { return }
        learning = nil
        onSpeakerCommand?(.list)
    }

    /// Called by `AppDelegate` when a `speakers` reply arrives.
    func speakersChanged(to profiles: [Helper.SpeakerProfile]) {
        speakers = profiles
    }

    /// A voice was heard. Only used to notice someone *new* — the roster
    /// itself comes from the core, so this asks rather than guesses.
    func speakerHeard(id: String) {
        guard !speakers.contains(where: { $0.id == id }) else { return }
        onSpeakerCommand?(.list)
    }

    /// Where the core looks for the voiceprint model, in the same order.
    private static var speakerModelIsInstalled: Bool {
        var roots: [String] = []
        if let override = ProcessInfo.processInfo.environment["RANEEN_SPEAKER_DIR"] {
            roots.append(override)
        }
        roots.append(Bundle.main.bundlePath + "/Contents/Resources/helper")
        roots.append(NSHomeDirectory() + "/.cache/raneen/speaker")
        return roots.contains {
            FileManager.default.fileExists(atPath: $0 + "/campplus.onnx")
        }
    }

    /// Keep the stored settings self-consistent, then persist them.
    ///
    /// **Coerced here rather than while building argv**, so the window shows
    /// what the core will actually be given. Fixing it silently at the last
    /// moment would leave the language picker reading "Hindi" while the core
    /// ran in English — a disagreement the user has no way to see.
    ///
    /// Switching to an English-only model therefore drags the language back
    /// to English. Leaving them to disagree would not fail: an `.en` model
    /// given Hindi transliterates it into English phonemes and returns
    /// confident nonsense, which reads like a hallucination rather than a
    /// configuration mistake.
    private func normaliseAndSave(previous: HelperConfig) {
        if selectedModelIsEnglishOnly && config.language != "en" {
            // Re-enters this method once through `didSet`; the second pass
            // finds nothing to fix and does the saving.
            config.language = "en"
            return
        }

        // Changing the trigger moves the timings that depend on it — unless
        // they have been set to something of the user's own, which is what
        // comparing against the *old* trigger's recommendation detects. So a
        // deliberate 40-frame silence survives a mode switch, and an
        // untouched one follows the recommendation instead of quietly
        // applying push-to-talk timings to a wake word.
        if config.trigger != previous.trigger {
            var adjusted = config
            if previous.silenceFrames
                == HelperConfig.recommendedSilenceFrames(for: previous.trigger)
            {
                adjusted.silenceFrames = HelperConfig.recommendedSilenceFrames(for: config.trigger)
            }
            if previous.preRollFrames
                == HelperConfig.recommendedPreRollFrames(for: previous.trigger)
            {
                adjusted.preRollFrames = HelperConfig.recommendedPreRollFrames(for: config.trigger)
            }
            if adjusted != config {
                config = adjusted
                return
            }
        }

        SettingsStore.save(config, to: defaults)
    }

    /// Re-read what is on disk. Called when the window opens, so a model or
    /// wake-word file added since launch appears without a relaunch.
    func refreshLibraries() {
        models = ModelLibrary.available()
        wakeFeatureModelsAvailable = WakeWordLibrary.featureModelsAvailable()
        downloader.refresh()

        // A selection pointing at a file that is no longer there would be
        // passed to the core verbatim, and whisper failing to load is an
        // `error` event and no dictation at all. Falling back to the bundled
        // model keeps the app working; leaving the stale path would mean the
        // window and the core disagreeing about something that cannot work.
        if let path = config.modelPath, !FileManager.default.fileExists(atPath: path) {
            Log.app.error("selected model \(path) is gone — falling back to the bundled model")
            config.modelPath = nil
        }
    }

    // MARK: - The model catalogue

    /// Delete a downloaded model.
    ///
    /// The order matters: the selection is moved off the file *before* it is
    /// removed, so there is no window in which the stored settings name a
    /// model that does not exist.
    /// Only ever the copy in the download directory — the same file
    /// `canDelete` answers about. A model found inside the app bundle, or one
    /// added from somewhere else on disk, is not this app's to remove.
    func deleteModel(_ filename: String) {
        if config.modelPath == ModelInstall.destination(for: filename).path {
            config.modelPath = nil
        }
        downloader.delete(filename)
        refreshLibraries()
    }

    /// Models on disk that the catalogue does not know about — a model built
    /// or converted elsewhere, added through the open panel. Listed
    /// separately so the catalogue rows stay a fixed, familiar set.
    var addedModels: [WhisperModel] {
        let known = Set(ModelCatalog.all.map(\.filename))
        return models.filter { !known.contains($0.name) }
    }

    // MARK: - Editing

    func addWakeWord(_ path: String) {
        guard !config.wakeWords.contains(path) else { return }
        config.wakeWords.append(path)
    }

    func removeWakeWords(_ paths: Set<String>) {
        config.wakeWords.removeAll { paths.contains($0) }
    }

    /// The language a model can actually produce.
    ///
    /// An `.en` model given other speech does not fail — it transliterates
    /// into English phonemes and returns confident nonsense. So selecting
    /// one forces the language, rather than leaving a control that promises
    /// something impossible.
    var selectedModelIsEnglishOnly: Bool {
        guard let path = config.modelPath else {
            // No explicit choice means the core resolves its own, and the
            // bundled model is `base.en`.
            return true
        }
        return WhisperModel(path: path).isEnglishOnly
    }
}
