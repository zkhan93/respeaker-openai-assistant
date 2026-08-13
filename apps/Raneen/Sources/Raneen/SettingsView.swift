import AppKit
import SwiftUI

/// The settings window.
///
/// **SwiftUI, in an otherwise AppKit app.** A deliberate exception rather
/// than a drift: this is twenty-odd controls with dependencies between them
/// (an English-only model disables the language picker, an unarmed wake word
/// hides its tuning), which in AppKit is several hundred lines of target,
/// action and enablement bookkeeping. The menu bar, the status item and the
/// listening panel stay AppKit, where they belong — nothing there benefits
/// from a declarative rebuild, and `ListeningPanel` in particular depends on
/// window behaviour SwiftUI does not expose.
struct SettingsView: View {

    @ObservedObject var model: SettingsModel

    var body: some View {
        VStack(spacing: 0) {
            TabView {
                dictation.tabItem { Label("Dictation", systemImage: "mic") }
                transcription.tabItem { Label("Transcription", systemImage: "text.bubble") }
                detection.tabItem { Label("Detection", systemImage: "waveform") }
                wakeWord.tabItem { Label("Wake Word", systemImage: "ear") }
                recording.tabItem { Label("Recording", systemImage: "dot.radiowaves.left.and.right")
                }
            }
            .padding(.top, 8)

            Divider()
            footer
        }
        .frame(width: 580, height: 470)
        .onAppear { model.refreshLibraries() }
    }

    // MARK: - Dictation

    private var dictation: some View {
        Form {
            Section {
                Picker("When to listen", selection: $model.config.trigger) {
                    ForEach(TriggerMode.allCases, id: \.self) { mode in
                        Text(mode.label).tag(mode)
                    }
                }
                if model.config.trigger == .wakeword && model.config.wakeWords.isEmpty {
                    Label(
                        "No wake word is configured, so nothing will open a turn.",
                        systemImage: "exclamationmark.triangle"
                    )
                    .foregroundStyle(.orange)
                }
            } footer: {
                Text(
                    """
                    Holding a key ignores the voice detector entirely, so a pause \
                    for breath cannot split a sentence in two. The other modes let \
                    silence close the turn.
                    """
                )
                .font(.callout)
                .foregroundStyle(.secondary)
            }

            Section("Trigger key") {
                Text("Chosen from the menu bar — currently \(TriggerKey.current.label).")
                    .foregroundStyle(.secondary)
            }

            Section {
                HStack(alignment: .center, spacing: 20) {
                    Picker("", selection: $model.indicatorStyle) {
                        ForEach(IndicatorStyle.allCases) { style in
                            Text(style.label).tag(style)
                        }
                    }
                    .pickerStyle(.radioGroup)
                    .labelsHidden()

                    Spacer()
                    // The preview is the control, really — the names mean
                    // nothing until you have watched one.
                    IndicatorPreview(style: model.indicatorStyle)
                        .fixedSize()
                        .frame(width: 80, height: 80)
                }
                Text(model.indicatorStyle.detail)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } header: {
                Text("While listening")
            } footer: {
                // Said plainly because every other control on this window
                // needs the Apply button, and leaving this one to look the
                // same would have people restarting the core for a colour.
                Text("Takes effect immediately — the core is not involved.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }

    // MARK: - Transcription

    private var transcription: some View {
        Form {
            Section {
                Picker("Transcribe with", selection: $model.config.engine) {
                    ForEach(SttEngine.allCases, id: \.self) { engine in
                        Text(engine.label).tag(engine)
                    }
                }
            }

            if model.config.engine == .local {
                Section("Model") {
                    Picker("Whisper model", selection: $model.config.modelPath) {
                        Text("Bundled (base.en)").tag(String?.none)
                        ForEach(model.models) { candidate in
                            Text("\(candidate.name) — \(candidate.sizeDescription)")
                                .tag(String?.some(candidate.path))
                        }
                    }
                    Button("Add a Model File…") { chooseModel() }
                    Text(
                        """
                        Larger models are more accurate and slower. Models are found \
                        in ~/.cache/raneen/models and inside the app.
                        """
                    )
                    .font(.callout)
                    .foregroundStyle(.secondary)
                }
            } else {
                Section("Server") {
                    TextField("URL", text: $model.config.remoteURL, prompt: Text("http://nas.local:8000/v1"))
                    TextField("Model name", text: $model.config.remoteModel, prompt: Text("whisper-1"))
                    Toggle("Fall back to the local model on failure", isOn: $model.config.remoteFallback)
                    Text(
                        """
                        A self-hosted server needs no key. Reaching api.openai.com \
                        needs one, which arrives with the ZeroMQ security work — a \
                        key cannot be passed on a command line, where every process \
                        on this Mac could read it.
                        """
                    )
                    .font(.callout)
                    .foregroundStyle(.secondary)
                }
            }

            Section("Language") {
                Picker("Language", selection: $model.config.language) {
                    ForEach(Self.languages, id: \.code) { language in
                        Text(language.name).tag(language.code)
                    }
                }
                .disabled(model.selectedModelIsEnglishOnly)

                if model.selectedModelIsEnglishOnly {
                    // Not merely greyed out: the reason matters, because the
                    // failure is silent. An `.en` model given Hindi does not
                    // error — it transliterates into English phonemes and
                    // returns confident nonsense that reads like a
                    // hallucination.
                    Label(
                        "This model is English-only. Choose a model without “.en” "
                            + "in its name for other languages.",
                        systemImage: "info.circle"
                    )
                    .foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
    }

    // MARK: - Detection

    private var detection: some View {
        Form {
            Section {
                Picker("Voice detector", selection: $model.config.vad) {
                    ForEach(VadKind.allCases, id: \.self) { kind in
                        Text(kind.label).tag(kind)
                    }
                }
            } footer: {
                Text(
                    """
                    Silero rejects non-speech noise; on a door-slam-then-keys \
                    recording it opens one turn where the energy detector opens three.
                    """
                )
                .font(.callout)
                .foregroundStyle(.secondary)
            }

            // `Section("title") { … } footer: { … }` does not exist —
            // SwiftUI's string-title initialiser is the footerless one, so a
            // titled section with a footer has to spell out its header.
            Section {
                // Frames are the unit the core counts in, and milliseconds
                // are the unit a person thinks in, so both are shown.
                Stepper(value: $model.config.silenceFrames, in: 2...100) {
                    row(
                        "Silence before a turn ends",
                        Self.duration(frames: model.config.silenceFrames))
                }
                Stepper(value: $model.config.preRollFrames, in: 0...50) {
                    row(
                        "Audio kept from before it starts",
                        Self.duration(frames: model.config.preRollFrames))
                }
                LabeledContent("Longest single segment") {
                    Slider(value: $model.config.maxSeconds, in: 5...120, step: 5) {
                        Text("\(Int(model.config.maxSeconds))s").monospacedDigit()
                    }
                }
            } header: {
                HStack {
                    Text("Segmentation")
                    Spacer()
                    if model.timingsAreRecommended {
                        Text("recommended for “\(model.config.trigger.label)”")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else {
                        Button("Use Recommended") { model.useRecommendedTimings() }
                            .buttonStyle(.link)
                            .font(.caption)
                    }
                }
            } footer: {
                Text(
                    """
                    These follow the trigger: a wake word waits 2 s, because \
                    someone composing a request out loud pauses mid-sentence and \
                    ending the turn there hands back half of it. Holding a key waits \
                    640 ms, since you decide when the sentence is over.

                    Pre-roll exists because a detector reports about 240 ms after \
                    speech actually began — without it every turn clips its own \
                    first word. A key press is exact and needs almost none.
                    """
                )
                .font(.callout)
                .foregroundStyle(.secondary)
            }

            Section("Confidence gate") {
                LabeledContent("Discard below") {
                    Slider(value: $model.config.minConfidence, in: 0...0.9, step: 0.05) {
                        Text(
                            model.config.minConfidence == 0
                                ? "off" : String(format: "%.2f", model.config.minConfidence)
                        )
                        .monospacedDigit()
                    }
                }
                Text(
                    """
                    Leave this off unless nobody is watching the output. Low \
                    confidence usually means the model cannot represent the speech \
                    rather than that the audio was noise — a gate deletes real words \
                    and leaves no trace of them.
                    """
                )
                .font(.callout)
                .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }

    // MARK: - Wake word

    private var wakeWord: some View {
        Form {
            if !model.wakeFeatureModelsAvailable {
                Section {
                    // A first-class state, not an error. The two shared
                    // feature models are not shipped in the bundle, so every
                    // new user is here — and a core that exits at startup
                    // because of it would look like a bug.
                    Label(
                        "The shared openWakeWord models are not installed. Run "
                            + "tools/fetch-wakeword-models.sh — wake words cannot run "
                            + "without them, whatever is listed below.",
                        systemImage: "exclamationmark.triangle"
                    )
                    .foregroundStyle(.orange)
                }
            }

            Section {
                if model.config.wakeWords.isEmpty {
                    Text("None. Any openWakeWord classifier (.onnx) works.")
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(model.config.wakeWords, id: \.self) { path in
                        HStack {
                            Text((path as NSString).lastPathComponent)
                            Spacer()
                            Button {
                                model.removeWakeWords([path])
                            } label: {
                                Image(systemName: "minus.circle")
                            }
                            .buttonStyle(.borderless)
                        }
                    }
                }
                Button("Add a Wake Word Model…") { chooseWakeWord() }
            } header: {
                Text("Wake words")
            } footer: {
                Text(
                    """
                    A wake word is always reported to ZeroMQ consumers, in every \
                    mode. It only opens a turn when “When I say a wake word” is \
                    chosen on the Dictation tab — arming a detector never takes \
                    push-to-talk away.
                    """
                )
                .font(.callout)
                .foregroundStyle(.secondary)
            }

            if !model.config.wakeWords.isEmpty {
                Section("Tuning") {
                    LabeledContent("Threshold") {
                        Slider(value: $model.config.wakeThreshold, in: 0.05...0.95, step: 0.05) {
                            Text(String(format: "%.2f", model.config.wakeThreshold))
                                .monospacedDigit()
                        }
                    }
                    Stepper(value: $model.config.wakePatience, in: 1...10) {
                        row("Frames over threshold", "\(model.config.wakePatience)")
                    }
                    Stepper(value: $model.config.wakeCooldownFrames, in: 5...100) {
                        row(
                            "Ignore after firing",
                            Self.duration(frames: model.config.wakeCooldownFrames))
                    }
                    Text(
                        """
                        Lower the threshold to be more sensitive. Each extra frame of \
                        patience costs 80 ms of latency and rejects one more \
                        single-frame false positive. The cooldown exists because one \
                        spoken word crosses the threshold several times running.
                        """
                    )
                    .font(.callout)
                    .foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
    }

    // MARK: - Recording

    private var recording: some View {
        Form {
            Section {
                Picker("Publish audio and events", selection: $model.config.broadcast) {
                    ForEach(BroadcastMode.allCases, id: \.self) { mode in
                        Text(mode.label).tag(mode)
                    }
                }
                if model.config.broadcast != .off {
                    LabeledContent("Port") {
                        TextField(
                            "Port", value: $model.config.broadcastPort,
                            format: .number.grouping(.never)
                        )
                        .frame(width: 80)
                    }
                }
            } footer: {
                Text(
                    """
                    Speech-gated audio and every core event go out on a ZeroMQ PUB \
                    socket, for a recorder on a NAS or anything else on the network. \
                    It records but never transcribes, and silence publishes nothing. \
                    Dictation keeps working while this runs.
                    """
                )
                .font(.callout)
                .foregroundStyle(.secondary)
            }

            if model.config.broadcast == .network {
                Section {
                    Label(
                        "Anyone on this network can subscribe. There is no "
                            + "authentication yet, so the audio of this room is "
                            + "readable by every device that can reach this Mac.",
                        systemImage: "exclamationmark.shield"
                    )
                    .foregroundStyle(.orange)

                    // Not a surprise worth letting macOS spring on them: the
                    // prompt is the reason the core talks to this app over a
                    // pipe rather than TCP in the first place.
                    Label(
                        "macOS will ask whether Raneen may accept incoming network "
                            + "connections the first time this binds.",
                        systemImage: "info.circle"
                    )
                    .foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
    }

    // MARK: - Footer

    private var footer: some View {
        HStack {
            if model.isDirty {
                Label("The core is still running the previous settings.", systemImage: "clock")
                    .foregroundStyle(.secondary)
                    .font(.callout)
            } else if model.running != nil {
                Label("Running these settings.", systemImage: "checkmark.circle")
                    .foregroundStyle(.secondary)
                    .font(.callout)
            }
            Spacer()
            Button("Apply & Restart Core") { model.apply() }
                .keyboardShortcut(.defaultAction)
                .disabled(!model.isDirty)
        }
        .padding(12)
    }

    // MARK: - Pickers

    private func chooseModel() {
        let panel = NSOpenPanel()
        panel.title = "Choose a ggml Whisper Model"
        panel.allowedContentTypes = []
        panel.allowsOtherFileTypes = true
        panel.canChooseDirectories = false
        Self.start(panel, in: ModelLibrary.searchPaths)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        model.config.modelPath = url.path
        model.refreshLibraries()
    }

    private func chooseWakeWord() {
        let panel = NSOpenPanel()
        panel.title = "Choose an openWakeWord Classifier"
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        Self.start(panel, in: WakeWordLibrary.searchPaths)
        guard panel.runModal() == .OK else { return }
        for url in panel.urls { model.addWakeWord(url.path) }
    }

    /// Open the panel where the models actually are.
    ///
    /// **Both caches live under `~/.cache`, which is hidden**, so a panel
    /// opening at its default location cannot reach the files our own
    /// downloader wrote — the user has to know about ⌘⇧. or ⌘⇧G to get
    /// there. Pointing the panel at the directory is the whole fix.
    private static func start(_ panel: NSOpenPanel, in candidates: [URL]) {
        panel.showsHiddenFiles = true
        if let directory = candidates.first(where: {
            FileManager.default.fileExists(atPath: $0.path)
        }) {
            panel.directoryURL = directory
        }
    }

    // MARK: - Formatting

    /// A title with its current value trailing.
    ///
    /// Not `LabeledContent(_:value:)`: with a string literal title and a
    /// `String` value, that call resolves to the `content:` closure overload
    /// instead and fails to compile in a way that names neither argument.
    private func row(_ title: String, _ detail: String) -> some View {
        HStack {
            Text(title)
            Spacer()
            Text(detail).foregroundStyle(.secondary).monospacedDigit()
        }
    }

    /// Frames are 80 ms each. Shown as time because nobody thinks in frames,
    /// kept as frames underneath because that is what the core counts.
    private static func duration(frames: Int) -> String {
        let ms = frames * 80
        return ms >= 1000
            ? String(format: "%.1f s (%d)", Double(ms) / 1000, frames)
            : "\(ms) ms (\(frames))"
    }

    private static let languages: [(code: String, name: String)] = [
        ("en", "English"),
        ("auto", "Detect automatically"),
        ("ar", "Arabic"),
        ("de", "German"),
        ("es", "Spanish"),
        ("fr", "French"),
        ("hi", "Hindi"),
        ("it", "Italian"),
        ("ja", "Japanese"),
        ("nl", "Dutch"),
        ("pt", "Portuguese"),
        ("ru", "Russian"),
        ("ur", "Urdu"),
        ("zh", "Chinese"),
    ]
}
