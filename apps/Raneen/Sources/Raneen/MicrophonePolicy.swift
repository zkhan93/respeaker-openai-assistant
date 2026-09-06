import Foundation

/// When the microphone is allowed to be open.
///
/// **The shell owns the device (AD-16), so the shell decides when it is
/// open — and until AD-23 it never decided at all.** Capture started with
/// the helper and stopped at quit, in every trigger mode, so a hold-to-talk
/// user had a live microphone all day for a feature that listens for a few
/// seconds at a time. Nothing needed that. It was the shape AD-12 chose for
/// the Pi appliance ("gate the trigger, not the source"), carried over to a
/// Mac where the trigger is a key you are holding and the cost of a closed
/// device between presses is an engine start measured in tens of
/// milliseconds.
///
/// The rule is one sentence: **the microphone is open continuously only
/// when a feature needs continuous audio.** Otherwise it opens when the
/// trigger key goes down and closes when the core reports the turn over.
/// The reasons are enumerated rather than reduced to a boolean because the
/// settings window and the menu bar both say *why* — "always open" with no
/// cause is the thing that makes people uneasy, and the reasons are what
/// let them switch it off.
///
/// Derived from `HelperConfig` and nothing else, so it is a pure function
/// of what the core is about to be launched with, and a test can assert
/// every branch without a device.
enum MicrophonePolicy: Equatable {

    /// Open from the trigger key going down until the core reports the
    /// turn closed. The default, and what `hold` and `toggle` get when
    /// nothing else is switched on.
    case whileArmed

    /// Open for as long as the app runs, because at least one of these
    /// has to hear the room to do its job. Never empty.
    case continuous([Reason])

    /// Why the microphone cannot close between turns.
    ///
    /// Each one maps to a flag the core is launched with; a reason with no
    /// flag behind it would be a warning about nothing.
    enum Reason: Equatable, CaseIterable {
        /// `--trigger vad`: speech itself opens a turn.
        case speechTrigger
        /// `--trigger wakeword`: the wake word opens a turn.
        case wakeWordTrigger
        /// `--wake-word` in a keyed mode: reported to consumers, never
        /// obeyed, but scored on every frame all the same.
        case wakeWordArmed
        /// `--zmq-pub`: the always-on recorder.
        case recording
        /// The `--speaker-*` flags: the continuous speaker consumer.
        case speakerIdentification

        /// Read after "because", in the user's terms.
        var label: String {
            switch self {
            case .speechTrigger: return "the trigger listens for speech"
            case .wakeWordTrigger: return "the trigger is a wake word"
            case .wakeWordArmed: return "a wake word is armed"
            case .recording: return "recording is on"
            case .speakerIdentification: return "speaker identification is on"
            }
        }
    }

    init(for config: HelperConfig) {
        var reasons: [Reason] = []
        switch config.trigger {
        case .vad:
            reasons.append(.speechTrigger)
        case .wakeword:
            reasons.append(.wakeWordTrigger)
        case .hold, .toggle:
            // Reported in every mode and obeyed in one (see `HelperConfig`):
            // the detector runs whether or not it may open a turn, so
            // arming one is enough to keep the device open. Not listed
            // when the trigger *is* the wake word — that reason already
            // says it, and two lines for one fact reads as two problems.
            if !config.wakeWords.isEmpty { reasons.append(.wakeWordArmed) }
        }
        if config.broadcast != .off { reasons.append(.recording) }
        if config.identifySpeakers { reasons.append(.speakerIdentification) }
        self = reasons.isEmpty ? .whileArmed : .continuous(reasons)
    }

    var isContinuous: Bool {
        if case .continuous = self { return true }
        return false
    }

    var reasons: [Reason] {
        if case .continuous(let reasons) = self { return reasons }
        return []
    }

    /// The reasons as one clause: "a wake word is armed and recording is
    /// on". Always grammatical for one, two or five reasons, because this
    /// lands in a sentence the user reads rather than in a log.
    var reasonClause: String {
        let labels = reasons.map(\.label)
        switch labels.count {
        case 0: return ""
        case 1: return labels[0]
        default:
            return labels.dropLast().joined(separator: ", ") + " and " + labels[labels.count - 1]
        }
    }

    /// One line for the menu bar, which is the surface seen all day.
    ///
    /// - Parameter key: The trigger key's label, so the sentence names the
    ///   actual key rather than "the key".
    func summary(key: String) -> String {
        switch self {
        case .whileArmed:
            return "Mic opens only while you hold \(key)"
        case .continuous:
            return "Mic stays open: \(reasonClause)"
        }
    }
}
