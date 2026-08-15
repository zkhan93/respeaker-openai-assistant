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
///
/// **This file is composition only.** Every measurement, colour, card,
/// row and callout comes from `SettingsDesign.swift`, and the navigation
/// from `SettingsSidebar.swift`. That split is what stopped this file being
/// twenty bespoke `HStack`s: when a decision like "how wide is a control"
/// or "how loud is prose" has one answer somewhere else, a new section
/// cannot quietly invent a different one.
///
/// **Every paragraph of explanation the old window showed is still here**,
/// mostly behind a per-card disclosure. It documents behaviour that cost
/// real debugging — why pre-roll exists, why an `.en` model returns
/// confident nonsense, why the confidence gate should stay off, why a wake
/// word is reported in every mode and obeyed in one — and the reason it is
/// folded rather than deleted is that five paragraphs shown at once are
/// five paragraphs nobody reads.
struct SettingsView: View {

    @ObservedObject var model: SettingsModel

    /// Which section is showing. Window-lifetime state, deliberately not
    /// persisted: `SettingsWindow` keeps the window alive between openings,
    /// so re-opening returns you where you were within a run, and a fresh
    /// launch opens on Dictation — which is what the app is for.
    @State private var section: SettingsSection

    /// `initialSection` exists so a test can lay out every pane rather than
    /// only the one the window opens on. It is defaulted, so the app's one
    /// call site says nothing about it.
    init(model: SettingsModel, initialSection: SettingsSection = .dictation) {
        self.model = model
        _section = State(initialValue: initialSection)
    }

    var body: some View {
        HStack(spacing: 0) {
            SettingsSidebar(selection: $section)
            Divider()
            detail
        }
        .frame(width: SettingsMetric.windowSize.width, height: SettingsMetric.windowSize.height)
        .onAppear { model.refreshLibraries() }
    }

    /// The pane, and the one action beneath it.
    ///
    /// The action bar spans the pane rather than the whole window so the
    /// sidebar runs full height — the modern macOS arrangement, and it also
    /// keeps Apply visually attached to the settings it applies.
    private var detail: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: SettingsMetric.cardSpacing) {
                    SettingsPaneHeader(title: section.title, summary: section.summary)
                    pane
                }
                .padding(.horizontal, SettingsMetric.gutter)
                .padding(.top, SettingsMetric.titlebarInset)
                .padding(.bottom, SettingsMetric.gutter)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .background(SettingsPalette.pane)

            Divider()
            SettingsActionBar(status: status) {
                Button("Apply & Restart Core") { model.apply() }
                    .buttonStyle(.borderedProminent)
                    // Brand orange rather than the system accent, matching
                    // the sidebar selection: this window is the product, and
                    // it should look like one application.
                    .tint(SettingsPalette.brand)
                    .keyboardShortcut(.defaultAction)
                    .disabled(!model.isDirty)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder
    private var pane: some View {
        switch section {
        case .dictation: dictation
        case .transcription: transcription
        case .models: ModelsView(model: model)
        case .detection: detection
        case .wakeWord: wakeWord
        case .speakers: SpeakersView(model: model)
        case .recording: recording
        }
    }

    private var status: SettingsStatus? {
        if model.isDirty {
            return .pending("The core is still running the previous settings.")
        }
        if model.running != nil {
            return .settled("Running these settings.")
        }
        return nil
    }

    // MARK: - Dictation

    @ViewBuilder
    private var dictation: some View {
        SettingsCard(
            "Trigger",
            detailTitle: "Why the modes differ",
            detail: """
                Holding a key ignores the voice detector entirely, so a pause \
                for breath cannot split a sentence in two. The other modes let \
                silence close the turn.
                """
        ) {
            SettingsRow("When to listen", controlWidth: SettingsMetric.pickerWidth) {
                Picker("", selection: $model.config.trigger) {
                    ForEach(TriggerMode.allCases, id: \.self) { mode in
                        Text(mode.label).tag(mode)
                    }
                }
            }

            SettingsRow("Trigger key", caption: "Chosen from the menu bar.") {
                keyCap(TriggerKey.current.label)
            }

            if model.config.trigger == .wakeword && model.config.wakeWords.isEmpty {
                SettingsCallout(
                    .warning, "No wake word is configured, so nothing will open a turn.")
            }
        }

        SettingsCard("While listening") {
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(IndicatorStyle.allCases) { style in
                        SettingsOption(
                            title: style.label,
                            isSelected: model.indicatorStyle == style,
                            select: { model.indicatorStyle = style }
                        )
                    }
                }
                // Wide enough for the longest name and no wider. Filling
                // the row put a checkmark 250pt away from the word it
                // ticked, which made three one-word choices look like a
                // table of contents.
                .frame(width: 190)

                Spacer(minLength: 12)

                // The preview is the control, really — the names mean
                // nothing until you have watched one. Sized close to the
                // largest style's 74pt panel rather than stretched: an
                // indicator floating in a wide box misrepresents how small
                // this thing is over your work, which is most of what the
                // preview is for.
                SettingsStage {
                    IndicatorPreview(style: model.indicatorStyle)
                        .frame(width: 150, height: 100)
                }
            }

            SettingsFootnote(model.indicatorStyle.detail)

            // Said plainly because every other control on this window
            // needs the Apply button, and leaving this one to look the
            // same would have people restarting the core for a colour.
            SettingsCallout(.info, "Takes effect immediately — the core is not involved.")
        }
    }

    /// The bound key, as a key rather than as a sentence.
    ///
    /// It is not editable here (the menu bar owns that binding), so a
    /// disabled control would be a lie about what can be clicked; a keycap
    /// reads as a fact instead.
    private func keyCap(_ text: String) -> some View {
        Text(text)
            .font(SettingsType.control)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(
                SettingsPalette.quietFill,
                in: RoundedRectangle(cornerRadius: 5, style: .continuous)
            )
    }

    // MARK: - Transcription

    @ViewBuilder
    private var transcription: some View {
        SettingsCard("Engine") {
            SettingsRow("Transcribe with", controlWidth: SettingsMetric.pickerWidth) {
                Picker("", selection: $model.config.engine) {
                    ForEach(SttEngine.allCases, id: \.self) { engine in
                        Text(engine.label).tag(engine)
                    }
                }
            }
        }

        if model.config.engine == .local {
            modelCard
        } else {
            serverCard
        }

        SettingsCard("Language") {
            SettingsRow("Spoken language", controlWidth: SettingsMetric.pickerWidth) {
                Picker("", selection: $model.config.language) {
                    ForEach(Self.languages, id: \.code) { language in
                        Text(language.name).tag(language.code)
                    }
                }
                .disabled(model.selectedModelIsEnglishOnly)
            }

            if model.selectedModelIsEnglishOnly {
                // Not merely greyed out: the reason matters, because the
                // failure is silent. An `.en` model given Hindi does not
                // error — it transliterates into English phonemes and
                // returns confident nonsense that reads like a
                // hallucination.
                SettingsCallout(
                    .info,
                    "This model is English-only. Choose a model without “.en” "
                        + "in its name for other languages.")
            }
        }
    }

    /// A summary and a way through, not a picker.
    ///
    /// The choice itself moved to its own pane, because it grew a size, a
    /// download, a progress bar and a delete — none of which a pop-up menu
    /// can show. What stays here is the fact you came looking for: which
    /// model is in use. The platform pattern for that is a row that names the
    /// current value and a button that goes where it is changed.
    private var modelCard: some View {
        SettingsCard("Model") {
            SettingsRow(currentModelName, caption: currentModelWhere) {
                Button("Manage Models…") { section = .models }
            }

            SettingsFootnote(
                """
                Larger models are more accurate and slower. Twelve are \
                available to download, from a 32 MB tiny to a 3.1 GB large-v3.
                """
            )
        }
    }

    /// The model in use, by name.
    ///
    /// `nil` means the core resolves one itself, and its answer is the
    /// bundled `base.en` — so this says that rather than "None", which would
    /// read as "no model" while dictation plainly works.
    private var currentModelName: String {
        guard let path = model.config.modelPath else { return ModelInstall.defaultFilename }
        return (path as NSString).lastPathComponent
    }

    private var currentModelWhere: String {
        guard let path = model.config.modelPath else { return "Included with Raneen" }
        return (path as NSString).deletingLastPathComponent
    }

    private var serverCard: some View {
        SettingsCard(
            "Server",
            detailTitle: "About keys",
            detail: """
                A self-hosted server needs no key. Reaching api.openai.com \
                needs one, which arrives with the ZeroMQ security work — a \
                key cannot be passed on a command line, where every process \
                on this Mac could read it.
                """
        ) {
            SettingsRow("URL", controlWidth: SettingsMetric.fieldWidth) {
                TextField("URL", text: $model.config.remoteURL, prompt: Text("http://nas.local:8000/v1"))
            }
            SettingsRow("Model name", controlWidth: SettingsMetric.fieldWidth) {
                TextField("Model name", text: $model.config.remoteModel, prompt: Text("whisper-1"))
            }
            SettingsRow("Fall back to the local model on failure") {
                Toggle("", isOn: $model.config.remoteFallback)
                    // A switch, not the checkbox `Toggle` defaults to on
                    // macOS: this is a capability being turned on, which is
                    // what a switch means, and it is also what every
                    // settings surface on the platform now uses. Tinted with
                    // the brand so the one saturated control in the window
                    // is not a different colour from the rest of it.
                    .toggleStyle(.switch)
                    .tint(SettingsPalette.brand)
            }
        }
    }

    // MARK: - Detection

    @ViewBuilder
    private var detection: some View {
        SettingsCard(
            "Voice detector",
            detailTitle: "How the two differ",
            detail: """
                Silero rejects non-speech noise; on a door-slam-then-keys \
                recording it opens one turn where the energy detector opens three.
                """
        ) {
            SettingsRow("Detect speech with", controlWidth: SettingsMetric.pickerWidth) {
                Picker("", selection: $model.config.vad) {
                    ForEach(VadKind.allCases, id: \.self) { kind in
                        Text(kind.label).tag(kind)
                    }
                }
            }
        }

        SettingsCard(
            "Segmentation",
            detailTitle: "Why these numbers",
            detail: """
                These follow the trigger: a wake word waits 2 s, because \
                someone composing a request out loud pauses mid-sentence and \
                ending the turn there hands back half of it. Holding a key waits \
                640 ms, since you decide when the sentence is over.

                Pre-roll exists because a detector reports about 240 ms after \
                speech actually began — without it every turn clips its own \
                first word. A key press is exact and needs almost none.
                """,
            accessory: {
                if model.timingsAreRecommended {
                    Text("recommended for “\(model.config.trigger.label)”")
                        .font(SettingsType.caption)
                        .foregroundStyle(.secondary)
                } else {
                    // A small bordered button rather than the `.link` style
                    // it was: a link paints itself in the system accent,
                    // which put the window's only patch of blue in the
                    // middle of an otherwise orange surface — and it read as
                    // a hyperlink to somewhere rather than as an action on
                    // the two rows underneath it.
                    Button("Use Recommended") { model.useRecommendedTimings() }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                }
            }
        ) {
            // Frames are the unit the core counts in, and milliseconds are
            // the unit a person thinks in, so both are shown — the time as
            // the value being set, the frames as a caption, which is the
            // hierarchy the old `2.0 s (25)` did not have.
            SettingsRow(
                "Silence before a turn ends",
                caption: SettingsFormat.frameCount(model.config.silenceFrames),
                value: SettingsFormat.duration(frames: model.config.silenceFrames)
            ) {
                Stepper("", value: $model.config.silenceFrames, in: 2...100)
            }

            SettingsRow(
                "Audio kept from before it starts",
                caption: SettingsFormat.frameCount(model.config.preRollFrames),
                value: SettingsFormat.duration(frames: model.config.preRollFrames)
            ) {
                Stepper("", value: $model.config.preRollFrames, in: 0...50)
            }

            SettingsRow(
                "Longest single segment",
                value: SettingsFormat.seconds(model.config.maxSeconds),
                controlWidth: SettingsMetric.sliderWidth
            ) {
                Slider(value: $model.config.maxSeconds, in: 5...120, step: 5)
            }
        }

        SettingsCard(
            "Confidence gate",
            detailTitle: "Why it should stay off",
            detail: """
                Low confidence usually means the model cannot represent the \
                speech rather than that the audio was noise — a gate deletes \
                real words and leaves no trace of them.
                """
        ) {
            SettingsRow(
                "Discard below",
                value: SettingsFormat.confidence(model.config.minConfidence),
                controlWidth: SettingsMetric.sliderWidth
            ) {
                Slider(value: $model.config.minConfidence, in: 0...0.9, step: 0.05)
            }

            // The instruction stays in the open and the reasoning folds
            // away: this is the one control on the window whose right
            // setting is "do not touch it", and a warning nobody expands
            // is not a warning.
            SettingsCallout(.info, "Leave this off unless nobody is watching the output.")
        }
    }

    // MARK: - Wake word

    @ViewBuilder
    private var wakeWord: some View {
        if !model.wakeFeatureModelsAvailable {
            // A first-class state, not an error. The two shared feature
            // models are not shipped in the bundle, so every new user is
            // here — and a core that exits at startup because of it would
            // look like a bug. Above the cards rather than inside one:
            // nothing below works until it is fixed.
            SettingsCallout(
                .warning,
                "The shared openWakeWord models are not installed. Run "
                    + "tools/fetch-wakeword-models.sh — wake words cannot run "
                    + "without them, whatever is listed below.")
        }

        SettingsCard(
            "Wake words",
            detailTitle: "Where a wake word is obeyed",
            detail: """
                A wake word is always reported to ZeroMQ consumers, in every \
                mode. It only opens a turn when “When I say a wake word” is \
                chosen on the Dictation tab — arming a detector never takes \
                push-to-talk away.
                """
        ) {
            if model.config.wakeWords.isEmpty {
                SettingsFootnote("None. Any openWakeWord classifier (.onnx) works.")
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(model.config.wakeWords.enumerated()), id: \.element) {
                        index, path in
                        if index > 0 { SettingsDivider() }
                        wakeWordRow(path)
                    }
                }
            }

            HStack {
                Spacer(minLength: 0)
                Button("Add a Wake Word Model…") { chooseWakeWord() }
            }
        }

        if !model.config.wakeWords.isEmpty {
            SettingsCard(
                "Tuning",
                detailTitle: "How to tune it",
                detail: """
                    Lower the threshold to be more sensitive. Each extra frame of \
                    patience costs 80 ms of latency and rejects one more \
                    single-frame false positive. The cooldown exists because one \
                    spoken word crosses the threshold several times running.
                    """
            ) {
                SettingsRow(
                    "Threshold",
                    value: SettingsFormat.threshold(model.config.wakeThreshold),
                    controlWidth: SettingsMetric.sliderWidth
                ) {
                    Slider(value: $model.config.wakeThreshold, in: 0.05...0.95, step: 0.05)
                }

                SettingsRow(
                    "Frames over threshold",
                    value: "\(model.config.wakePatience)"
                ) {
                    Stepper("", value: $model.config.wakePatience, in: 1...10)
                }

                SettingsRow(
                    "Ignore after firing",
                    caption: SettingsFormat.frameCount(model.config.wakeCooldownFrames),
                    value: SettingsFormat.duration(frames: model.config.wakeCooldownFrames)
                ) {
                    Stepper("", value: $model.config.wakeCooldownFrames, in: 5...100)
                }
            }
        }
    }

    /// One installed classifier.
    ///
    /// The directory is shown under the name because two useful wake-word
    /// files are routinely called the same thing — one in the repo's
    /// `models/`, one in `~/.cache/raneen` — and a list of bare filenames
    /// cannot tell you which one is armed.
    private func wakeWordRow(_ path: String) -> some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text((path as NSString).lastPathComponent)
                    .font(SettingsType.label)
                Text((((path as NSString).deletingLastPathComponent) as NSString)
                    .abbreviatingWithTildeInPath)
                    .font(SettingsType.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: 8)
            Button {
                model.removeWakeWords([path])
            } label: {
                Image(systemName: "minus.circle")
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.borderless)
            .help("Remove this wake word")
        }
    }

    // MARK: - Recording

    @ViewBuilder
    private var recording: some View {
        SettingsCard(
            "Publishing",
            detailTitle: "What goes out",
            detail: """
                Speech-gated audio and every core event go out on a ZeroMQ PUB \
                socket, for a recorder on a NAS or anything else on the network. \
                It records but never transcribes, and silence publishes nothing. \
                Dictation keeps working while this runs.
                """
        ) {
            SettingsRow("Publish audio and events", controlWidth: SettingsMetric.pickerWidth) {
                Picker("", selection: $model.config.broadcast) {
                    ForEach(BroadcastMode.allCases, id: \.self) { mode in
                        Text(mode.label).tag(mode)
                    }
                }
            }

            if model.config.broadcast != .off {
                SettingsRow("Port", controlWidth: 90) {
                    TextField(
                        "Port", value: $model.config.broadcastPort,
                        format: .number.grouping(.never)
                    )
                }
            }

            if model.config.broadcast == .network {
                SettingsCallout(
                    .security,
                    "Anyone on this network can subscribe. There is no "
                        + "authentication yet, so the audio of this room is "
                        + "readable by every device that can reach this Mac.")

                // Not a surprise worth letting macOS spring on them: the
                // prompt is the reason the core talks to this app over a
                // pipe rather than TCP in the first place.
                SettingsCallout(
                    .info,
                    "macOS will ask whether Raneen may accept incoming network "
                        + "connections the first time this binds.")
            }
        }
    }

    // MARK: - Pickers

    /// The whisper-model panel moved to `ModelsView` with the rest of the
    /// model library. This one stays because wake words have no catalogue: a
    /// classifier still arrives as a file the user found.
    private func chooseWakeWord() {
        let panel = NSOpenPanel()
        panel.title = "Choose an openWakeWord Classifier"
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.reveal(WakeWordLibrary.searchPaths)
        guard panel.runModal() == .OK else { return }
        for url in panel.urls { model.addWakeWord(url.path) }
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
