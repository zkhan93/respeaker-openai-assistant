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
            IndicatorPreference.save(indicatorStyle)
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

    /// Applying means restarting the core. Owned by `AppDelegate` because
    /// process lifecycle is its job, not this type's.
    var onApply: (() -> Void)?

    init() {
        config = SettingsStore.current()
        indicatorStyle = IndicatorPreference.current()
        models = ModelLibrary.available()
        wakeFeatureModelsAvailable = WakeWordLibrary.featureModelsAvailable()

        // **Nothing is written here.** `current()` already repairs what it
        // reads, and an earlier version called `normaliseAndSave()` at this
        // point — which persisted values nobody had chosen. Combined with
        // reading before defaults were registered, that turned a momentary
        // bad read into a permanently broken configuration on disk. Writing
        // settings is now something only an edit does.
        if selectedModelIsEnglishOnly { config.language = "en" }
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

        SettingsStore.save(config)
    }

    /// Re-read what is on disk. Called when the window opens, so a model or
    /// wake-word file added since launch appears without a relaunch.
    func refreshLibraries() {
        models = ModelLibrary.available()
        wakeFeatureModelsAvailable = WakeWordLibrary.featureModelsAvailable()
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
