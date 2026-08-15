import Foundation

/// Everything the core is told at launch, and the one place settings
/// become command-line flags.
///
/// ## Why argv rather than a settings file the core reads
///
/// The core is a pure function of its command line. That is what lets
/// `protocol/` assert on its behaviour from a command line alone — the
/// moment it reads a file from the user's home directory, a machine's
/// contents change what the conformance suite is testing, and "it passes
/// on my box" becomes possible. It also avoids inventing precedence rules
/// between flag, file and environment for every setting forever.
///
/// So the shell owns settings (`UserDefaults`, the native answer, already
/// used for the trigger key and the device choice) and translates them
/// into argv. Applying a change means restarting the core, which costs
/// ~0.05 s because it is Rust — see `AppDelegate.restartHelper`.
///
/// ## What must never go in argv
///
/// **Secrets.** `ps` shows full command lines to every process on the
/// machine, so an API key or a ZeroMQ auth key passed as a flag would be
/// readable by anything running as the user. Nothing here emits one, and
/// nothing should be added that does.
///
/// This is why there is no API-key field yet. A self-hosted server needs
/// no key — which is the case this is for — and the core already reads
/// `OPENAI_API_KEY` from the environment it inherits, which covers running
/// from a terminal. Reaching `api.openai.com` from the bundled app needs
/// the key in the Keychain, and that arrives with the ZeroMQ auth work so
/// that every secret in the product is handled once, the same way.
struct HelperConfig: Equatable {

    // MARK: - Dictation

    var trigger: TriggerMode = .hold

    // MARK: - Transcription

    var engine: SttEngine = .local
    /// `nil` means "whatever the core finds" — the model inside the
    /// bundle, then `~/.cache/raneen/models`. Deliberately not resolved
    /// here: the core already has that search order, and duplicating it
    /// would give two answers to one question.
    var modelPath: String?
    var language: String = "en"
    var remoteURL: String = ""
    var remoteModel: String = ""
    /// Fall back to the local model when a remote engine fails, so a dead
    /// network costs accuracy rather than the sentence just spoken.
    var remoteFallback: Bool = true

    // MARK: - Voice detection and segmentation

    var vad: VadKind = .silero
    /// Frames of silence before a turn closes. 80 ms each.
    ///
    /// Defaulted for `.hold`, the default trigger. Switching trigger moves it
    /// — see `recommendedSilenceFrames`.
    var silenceFrames: Int = recommendedSilenceFrames(for: .hold)
    /// Audio kept from before the turn opened. 80 ms each.
    var preRollFrames: Int = recommendedPreRollFrames(for: .hold)
    var maxSeconds: Double = 30
    /// 0 disables the gate. See `Policy::min_confidence` for why that is
    /// the right default: low confidence usually means the model cannot
    /// represent the speech, and silently deleting real words is worse
    /// than passing through a bad transcript the user can see and fix.
    var minConfidence: Double = 0

    // MARK: - Wake word

    var wakeWords: [String] = []
    var wakeThreshold: Double = 0.5
    var wakePatience: Int = 1
    var wakeCooldownFrames: Int = 25

    // MARK: - Always-on recording

    var broadcast: BroadcastMode = .off
    var broadcastPort: Int = 5555

    // MARK: - Speaker identification

    /// Whether the core tracks who is speaking.
    ///
    /// **Off by default, and the reason is memory rather than taste.** The
    /// voiceprint model costs about 125 MB resident for as long as it is
    /// loaded, whether or not anyone ever speaks — roughly tripling an app
    /// that otherwise idles around 65 MB. Nobody should pay that without
    /// asking for it.
    var identifySpeakers: Bool = false
    /// Seconds of speech per voiceprint. Speech shorter than this is not
    /// identified at all, rather than identified badly.
    ///
    /// **Only multiples of 2 are valid**, which is why the window offers
    /// four fixed choices rather than a slider. CAM++ pools time in
    /// 2-second segments and pads a partial one with zeros, so a 2.5 s
    /// window computes a quarter of its context from silence and two
    /// different people come out scoring 0.95. The core rounds anything
    /// else, and an earlier version of this pane shipped a 0.5-step
    /// slider whose every other position was quietly broken.
    var speakerWindow: Double = 4.0
    /// Seconds between re-identifications while one person keeps talking.
    var speakerInterval: Double = 2.0
    /// Seconds of quiet a voiceprint may span before it starts over.
    ///
    /// **This is what makes dictation identifiable at all.** A voiceprint
    /// needs a whole window of speech, the window can only be 2/4/6/8 s,
    /// and dictation turns run two to four seconds — so requiring one
    /// unbroken turn identified nobody. Carrying across short pauses
    /// makes the window "the last few seconds you spoke". Not in the
    /// pane: the trade it controls (short turns against telling apart two
    /// people who alternate quickly) is a room-shaped decision, and this
    /// app's room is one person at a keyboard.
    var speakerGap: Double = 2.0
    /// How alike two recordings must be to count as the same person.
    ///
    /// **Lower merges, higher splits**, which is the opposite of how a
    /// "match strength" slider usually reads and the reason the window
    /// labels it by consequence rather than by number. Someone whose
    /// single voice keeps becoming three speakers wants this *lower*.
    ///
    /// 40% comes from ten real recordings at the 4 s window — see the
    /// core's `DEFAULT_MATCH_THRESHOLD`. It replaced a guessed 65%, which
    /// sat *below* the worst same-person score and so turned one person
    /// into several. Two earlier estimates were measured at window
    /// lengths that silently corrupt the embedding; re-derive with
    /// `raneen-core voiceprint` rather than adjusting by feel.
    var speakerThreshold: Double = 0.40

    // MARK: - Values that depend on the trigger

    /// How long a pause is tolerated before the turn closes, per trigger.
    ///
    /// **Mirrors `Policy::dictation` in the core, and has to.** Because argv
    /// always carries `--silence-frames`, the core's own mode-dependent
    /// default can never take effect from this app — whatever is here wins.
    /// A single number for all modes therefore is not a neutral choice: it
    /// silently overrides the core.
    static func recommendedSilenceFrames(for trigger: TriggerMode) -> Int {
        switch trigger {
        // Someone who has just said a wake word is composing a request as
        // they speak. 640 ms of silence is an ordinary thinking pause, and
        // ending the turn there hands back half a sentence. 2 s only costs
        // latency.
        case .wakeword: return 25
        case .hold, .vad, .toggle: return 8
        }
    }

    /// Audio kept from before the turn opened, per trigger.
    ///
    /// A key press is an exact instant, so `hold` needs almost none. Every
    /// other trigger is a *detector*, which reports about 240 ms after speech
    /// actually began — so without pre-roll each turn clips its own first
    /// word. Phase 1 sent the `hold` value in every mode and did exactly
    /// that.
    static func recommendedPreRollFrames(for trigger: TriggerMode) -> Int {
        switch trigger {
        case .hold: return 3
        case .vad, .toggle, .wakeword: return 10
        }
    }

    // MARK: - Building the command line

    /// The flags to launch `serve` with.
    ///
    /// Every setting is emitted explicitly rather than only when it
    /// differs from the core's default. That makes the log line on spawn
    /// the whole configuration, which is worth more when diagnosing "why
    /// is it behaving like that" than a shorter command is.
    func argv(audioSocket: String) -> [String] {
        var args = ["serve"]

        // Positional, and first: the core reads argument 0 as the model.
        if let modelPath, !modelPath.isEmpty {
            args.append(modelPath)
        }

        args += ["--audio-socket", audioSocket]
        // The shell owns the earcons (AD-16), so the core stays quiet. Its
        // output device would otherwise be fixed at startup and keep
        // beeping into whatever was plugged in then.
        args.append("--no-sound")

        args += ["--trigger", trigger.rawValue]
        args += ["--vad", vad.rawValue]
        args += ["--language", language]
        args += ["--silence-frames", String(silenceFrames)]
        args += ["--pre-roll-frames", String(preRollFrames)]
        args += ["--max-seconds", trimmed(maxSeconds)]
        args += ["--min-confidence", trimmed(minConfidence)]

        switch engine {
        case .local:
            args += ["--stt", "local"]
        case .remote, .realtime:
            // The URL's scheme picks the engine on its own, so passing both
            // creates a state where they can disagree. Pass the URL when
            // there is one and let it decide.
            if remoteURL.isEmpty {
                args += ["--stt", engine.rawValue]
            } else {
                args += ["--stt-url", remoteURL]
            }
            if !remoteModel.isEmpty {
                args += ["--stt-model", remoteModel]
            }
            if !remoteFallback {
                args += ["--stt-fallback", "none"]
            }
        }

        // A wake word is reported in every trigger mode and obeyed in one.
        // Arming a detector here does not take push-to-talk away.
        for path in wakeWords where !path.isEmpty {
            args += ["--wake-word", path]
        }
        if !wakeWords.isEmpty {
            args += ["--wake-threshold", trimmed(wakeThreshold)]
            args += ["--wake-patience", String(wakePatience)]
            args += ["--wake-cooldown", String(wakeCooldownFrames)]
        }

        if let endpoint = broadcast.endpoint(port: broadcastPort) {
            args += ["--zmq-pub", endpoint]
        }

        if identifySpeakers {
            args += ["--speaker-window", trimmed(speakerWindow)]
            args += ["--speaker-interval", trimmed(speakerInterval)]
            args += ["--speaker-threshold", String(format: "%.2f", speakerThreshold)]
            args += ["--speaker-gap", trimmed(speakerGap)]
            args += ["--speaker-store", HelperConfig.speakerStorePath]
        }

        return args
    }

    /// Where voiceprints live.
    ///
    /// Application Support rather than the model cache: these are *user
    /// data*, not a redownloadable artefact. Clearing caches must not
    /// forget who anyone is.
    static var speakerStorePath: String {
        let base = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)
            .first?
            .appendingPathComponent("Raneen", isDirectory: true)
        if let base {
            try? FileManager.default.createDirectory(
                at: base, withIntermediateDirectories: true)
            return base.appendingPathComponent("speakers.json").path
        }
        return NSHomeDirectory() + "/.raneen-speakers.json"
    }

    /// `30.0` reads as a mistake in a command line; `30` does not.
    private func trimmed(_ value: Double) -> String {
        value == value.rounded() && abs(value) < 1e9
            ? String(Int(value))
            : String(format: "%g", value)
    }
}

// MARK: - Choices

/// Who decides a turn has started and ended — the core's `TriggerMode`.
enum TriggerMode: String, CaseIterable {
    case hold, vad, toggle, wakeword

    var label: String {
        switch self {
        case .hold: return "Hold a key to talk"
        case .vad: return "Whenever I speak"
        case .toggle: return "Toggle on and off"
        case .wakeword: return "When I say a wake word"
        }
    }
}

enum VadKind: String, CaseIterable {
    case silero, energy

    var label: String {
        switch self {
        case .silero: return "Silero (neural)"
        case .energy: return "Energy (no model)"
        }
    }
}

enum SttEngine: String, CaseIterable {
    case local, remote, realtime

    var label: String {
        switch self {
        case .local: return "On this Mac"
        case .remote: return "A server (batch)"
        case .realtime: return "OpenAI Realtime (streaming)"
        }
    }
}

/// Where speech-gated audio and events are published, if anywhere.
///
/// Three states rather than a checkbox because "on" hides the only
/// distinction that matters: a socket bound to loopback is reachable by
/// this Mac, and one bound to every interface is reachable by the network.
/// A dictation Mac quietly publishing a live room microphone to a café LAN
/// is the worst thing this feature can do, and it should take a deliberate
/// choice rather than a default.
enum BroadcastMode: String, CaseIterable {
    case off, loopback, network

    var label: String {
        switch self {
        case .off: return "Off"
        case .loopback: return "This Mac only"
        case .network: return "Anyone on my network"
        }
    }

    func endpoint(port: Int) -> String? {
        switch self {
        case .off: return nil
        case .loopback: return "tcp://127.0.0.1:\(port)"
        case .network: return "tcp://*:\(port)"
        }
    }
}
