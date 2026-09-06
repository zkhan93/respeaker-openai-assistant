import SwiftUI

/// Who the app has learned to recognise.
///
/// **People are enrolled on purpose, not discovered.** An earlier version
/// of this pane said the opposite — the core minted a profile whenever it
/// heard a voice it did not recognise, and this window offered to name
/// it. That was wrong in practice and the reason is worth keeping: a
/// failed match means either a new person *or* a poor recording of
/// somebody already enrolled, and nothing in the audio distinguishes
/// them. Assuming "new person" every time filled the registry with
/// fragments of one voice, and each fragment made the next match more
/// ambiguous, which made the next failure likelier.
///
/// So an unrecognised voice is now reported as `unknown` and nothing is
/// written. Getting in takes someone pressing a button and speaking,
/// which also solves the naming problem the old flow had: the person
/// enrolling knows who they are, where a match score is guessing.
///
/// Enrolling the same name twice improves that profile rather than
/// adding a second — the cheapest way to make recognition more reliable,
/// since each sample averages another few seconds into the centroid.
struct SpeakersView: View {

    @ObservedObject var model: SettingsModel
    @StateObject private var clips = SpeakerClipPlayer()
    @State private var editing: String?
    @State private var adding = false
    @State private var draftName: String = ""

    /// Whether something else keeps the microphone open. Decides how the
    /// pane says to enrol someone: speaking is enough when the room is
    /// heard, and otherwise they speak while the key is held.
    private var microphoneIsContinuous: Bool { MicrophonePolicy(for: model.config).isContinuous }

    private var trimmedDraft: String {
        draftName.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func beginLearning() {
        guard !trimmedDraft.isEmpty else { return }
        model.learnSpeaker(named: trimmedDraft)
        adding = false
        draftName = ""
    }

    var body: some View {
        Group {
            SettingsCard(
                "Recognising speakers",
                detailTitle: "What this costs",
                detail: """
                    The voiceprint model stays loaded while this is on and \
                    costs about 125 MB of memory whether or not anyone \
                    speaks — roughly tripling an app that otherwise idles \
                    around 65 MB. It never changes what dictation does: it \
                    listens alongside, and the hotkey behaves exactly as it \
                    did before.
                    """
            ) {
                SettingsRow("Identify who is speaking") {
                    SettingsSwitch(isOn: $model.config.identifySpeakers)
                }

                if model.config.identifySpeakers && !microphoneIsContinuous {
                    // Said because the first version of this pane claimed
                    // the opposite. Identification runs on the turns it is
                    // given; it does not keep the device open (AD-23).
                    SettingsCallout(
                        .info,
                        "Identifies whoever speaks during a turn. The microphone still "
                            + "closes between turns.")

                    // **Four fixed choices, not a slider.** The model
                    // pools time in 2-second segments and pads a partial
                    // one with zeros, so anything between these values
                    // does not degrade — it corrupts, and two different
                    // people come out scoring 0.95. The previous version
                    // of this row was a 0.5-step slider, which meant most
                    // of its positions were quietly broken.
                    SettingsRow(
                        "Voiceprint length",
                        caption: "Speech shorter than this is not identified at all, "
                            + "rather than badly. Longer recognises people more "
                            + "reliably but skips more of what they say — most "
                            + "dictation turns are under four seconds.",
                        // The same width as every other pop-up in the window,
                        // so the segmented control ends where the pickers do.
                        controlWidth: SettingsMetric.pickerWidth
                    ) {
                        Picker("", selection: $model.config.speakerWindow) {
                            ForEach(SettingsStore.Limits.speakerWindowChoices, id: \.self) {
                                Text("\(Int($0)) s").tag($0)
                            }
                        }
                        .pickerStyle(.segmented)
                    }

                    // The value sits in the row's value column, like every
                    // slider on Detection, rather than in a private HStack
                    // with its own width: the number is what is being read,
                    // and it lands in the same place on every pane.
                    SettingsRow(
                        "Check again every",
                        caption: "People interrupt and hand over mid-sentence, so this "
                            + "re-checks while someone is still talking rather than "
                            + "deciding once per turn.",
                        value: String(format: "%.1f s", model.config.speakerInterval),
                        controlWidth: SettingsMetric.sliderWidth
                    ) {
                        Slider(value: $model.config.speakerInterval, in: 0.5...10, step: 0.5)
                    }

                    // **The slider runs backwards from how it reads.** Its
                    // number is how alike two recordings must be, so a
                    // bigger one recognises fewer people as themselves and
                    // therefore produces *more* profiles. The caption is
                    // written in consequences rather than in similarity
                    // for exactly that reason.
                    SettingsRow(
                        "Same person if at least",
                        caption: "Drag left when one person keeps turning into several. "
                            + "Drag right when two people end up sharing a profile. "
                            + "Existing profiles are left alone — this only changes "
                            + "what happens from now on.",
                        value: "\(Int(model.config.speakerThreshold * 100))%",
                        controlWidth: SettingsMetric.sliderWidth
                    ) {
                        Slider(
                            value: $model.config.speakerThreshold,
                            in: SettingsStore.Limits.speakerThreshold,
                            step: 0.01
                        )
                    }

                    // Boxed as a note rather than left as a loose caption:
                    // it is the one line here that changes as the slider
                    // moves, and it should look like a reading, not a
                    // footnote that happens to be nearby.
                    SettingsCallout(.info, thresholdAdvice)
                }
            }

            if !model.speakerModelAvailable && model.config.identifySpeakers {
                SettingsCallout(
                    .warning,
                    "The voiceprint model is not on this Mac yet. Run "
                        + "./tools/fetch-speaker-models.sh — about 29 MB. Until then "
                        + "the core will refuse to start with identification on.")
            }

            if model.config.identifySpeakers {
                SettingsCard(
                    "People",
                    detailTitle: "How someone gets on this list",
                    detail: """
                        By being added and then speaking. Nobody is added \
                        automatically: a voice the core does not recognise is \
                        reported as unknown and nothing is stored, because a \
                        failed match is as often a poor recording of someone \
                        already here as it is a new person — and guessing wrong \
                        fills this list with fragments of one voice.

                        Adding the same person again does not make a second \
                        row. It adds a recording to theirs, which is the best \
                        way to make recognition more reliable.

                        Play a row to hear what it was built from. Deleting a \
                        profile deletes those recordings too.
                        """
                ) {
                    if model.speakers.isEmpty {
                        SettingsEmptyState(symbol: "person.crop.circle.badge.plus", emptyMessage)
                    } else {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(Array(model.speakers.enumerated()), id: \.element.id) {
                                index, profile in
                                if index > 0 { SettingsDivider() }
                                SpeakerRow(
                                    profile: profile,
                                    clips: clips,
                                    isEditing: editing == profile.id,
                                    draftName: $draftName,
                                    beginEditing: {
                                        draftName = profile.name ?? ""
                                        editing = profile.id
                                    },
                                    commit: { commit(profile) },
                                    cancelEditing: { editing = nil },
                                    improve: { name in model.learnSpeaker(named: name) },
                                    forget: {
                                        // Stop first: the core deletes the recording
                                        // along with the profile, and a player holding
                                        // a file that is about to vanish is a needless
                                        // race.
                                        if clips.playing == profile.id { clips.stop() }
                                        model.forgetSpeaker(profile.id)
                                    }
                                )
                            }
                        }
                    }

                    if let learning = model.learning {
                        // Tinted like the download bar on Models: this is the
                        // one thing in the pane that is actually happening,
                        // and it should be found without reading.
                        HStack(spacing: 10) {
                            ProgressView()
                                .controlSize(.small)
                                .tint(SettingsPalette.brand)
                            Text(
                                microphoneIsContinuous
                                    ? "Listening for \(learning) — say a couple of sentences."
                                    : "Listening for \(learning) — hold \(TriggerKey.current.label) "
                                        + "and say a couple of sentences.")
                                .font(SettingsType.label)
                                .foregroundStyle(.secondary)
                            Spacer(minLength: 8)
                            Button("Cancel") { model.cancelLearning() }
                        }
                        .padding(SettingsMetric.calloutPadding)
                        .background(
                            SettingsPalette.brand.opacity(0.08),
                            in: RoundedRectangle(
                                cornerRadius: SettingsMetric.calloutRadius, style: .continuous)
                        )
                    } else if adding {
                        HStack(spacing: 8) {
                            TextField("Name", text: $draftName)
                                .textFieldStyle(.roundedBorder)
                                .frame(maxWidth: 200)
                                .onSubmit { beginLearning() }
                            Spacer(minLength: 8)
                            Button("Cancel") { adding = false; draftName = "" }
                            Button("Start Listening") { beginLearning() }
                                .buttonStyle(.borderedProminent)
                                .tint(SettingsPalette.brand)
                                .keyboardShortcut(.defaultAction)
                                .disabled(trimmedDraft.isEmpty)
                        }
                    } else {
                        HStack {
                            Spacer(minLength: 0)
                            Button {
                                draftName = ""
                                adding = true
                            } label: {
                                Label("Add a Person…", systemImage: "plus")
                            }
                            .disabled(model.running?.identifySpeakers != true)
                            .help(model.running?.identifySpeakers == true
                                ? (microphoneIsContinuous
                                    ? "Type a name, then have them speak for a few seconds."
                                    : "Type a name, then hold \(TriggerKey.current.label) while "
                                        + "they speak for a few seconds.")
                                : "Apply the change first — the core has to be listening.")
                        }
                    }
                }
            }
        }
        .onAppear { model.refreshSpeakers() }
        // Leaving the pane must silence it. A voice still playing from a
        // window that is no longer showing has no visible stop button.
        .onDisappear { clips.stop() }
    }

    /// Nothing to show yet — but *why* differs, and the two need different
    /// answers from the user.
    private var emptyMessage: String {
        model.running?.identifySpeakers == true
            ? "Nobody yet. Add a person below, then have them speak for a few "
                + "seconds\(microphoneIsContinuous ? "" : " while the key is held"). "
                + "Until somebody is here, every voice is reported as unknown."
            : "Nobody yet. Apply the change, then add a person below."
    }

    /// What the current threshold means in practice, said plainly.
    ///
    /// A bare percentage tells nobody which way to drag it, and this is
    /// the one control here whose sensible value depends on the room
    /// rather than on a default anyone can pick correctly in advance.
    private var thresholdAdvice: String {
        switch model.config.speakerThreshold {
        case ..<0.32:
            return "Lenient. Voices are merged readily — expect few profiles, and "
                + "occasionally two people sharing one."
        case ..<0.52:
            return "Balanced. 40% is the default and it is measured, not chosen: "
                + "two people, ten recordings, scoring 0.52–0.86 against "
                + "themselves and 0.10–0.34 against each other."
        default:
            return "Strict. Above about 50% the same person starts scoring below "
                + "their own profile on a merely average recording, and becomes a "
                + "new speaker instead."
        }
    }

    private func commit(_ profile: Helper.SpeakerProfile) {
        let name = draftName.trimmingCharacters(in: .whitespacesAndNewlines)
        // An empty name is a cancel, not a request to store "". The core
        // would refuse it anyway; refusing here avoids the error round trip.
        if !name.isEmpty {
            model.nameSpeaker(profile.id, as: name)
        }
        editing = nil
    }
}

/// One enrolled voice: how to hear it, what it is called, and what to do
/// about it.
///
/// Its own view rather than a `row(for:)` on the pane so it can hold hover
/// state: the delete control appears under the pointer, as it does on
/// Models, because a column of permanently visible trash cans reads as a
/// list of things to destroy. Rename and Improve stay visible — for an
/// unnamed voice, "Name…" is the thing the row is asking for, and a button
/// that only exists on hover is a button nobody finds.
private struct SpeakerRow: View {

    let profile: Helper.SpeakerProfile
    @ObservedObject var clips: SpeakerClipPlayer
    let isEditing: Bool
    @Binding var draftName: String

    let beginEditing: () -> Void
    let commit: () -> Void
    let cancelEditing: () -> Void
    let improve: (String) -> Void
    let forget: () -> Void

    @State private var isHovered = false

    private var isNamed: Bool { profile.name != nil }

    private var recordings: String {
        "\(profile.samples) recording\(profile.samples == 1 ? "" : "s")"
    }

    var body: some View {
        HStack(spacing: 10) {
            // Playing the clip is the whole reason this row can be named,
            // so it takes the leading position the icon used to hold.
            // Profiles stored before clips existed have none; those keep
            // the plain icon rather than a button that would do nothing.
            if let clip = profile.clip {
                Button {
                    clips.toggle(profile.id, url: clip)
                } label: {
                    Image(systemName: clips.playing == profile.id ? "stop.circle.fill" : "play.circle")
                        .font(.system(size: 17))
                        .foregroundStyle(
                            clips.playing == profile.id ? SettingsPalette.brand : Color.secondary)
                }
                .buttonStyle(.plain)
                .help("Hear the recording this profile was created from.")
                .frame(width: 20)
            } else {
                Image(systemName: "person.wave.2")
                    .foregroundStyle(.secondary)
                    .frame(width: 20)
            }

            if isEditing {
                TextField("Name", text: $draftName)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 200)
                    .onSubmit(commit)
                Spacer(minLength: 8)
                Button("Cancel", action: cancelEditing)
                Button("Save", action: commit)
                    .buttonStyle(.borderedProminent)
                    .tint(SettingsPalette.brand)
                    .keyboardShortcut(.defaultAction)
            } else {
                VStack(alignment: .leading, spacing: 1) {
                    HStack(spacing: 6) {
                        Text(profile.name ?? profile.id)
                            .font(SettingsType.label)
                            .fontWeight(isNamed ? .medium : .regular)
                            .foregroundStyle(isNamed ? Color.primary : Color.secondary)
                        if !isNamed {
                            SettingsBadge("unnamed", quiet: true)
                        }
                    }
                    // The sample count is the honest measure of how well
                    // this profile is known, and it is why a fresh one
                    // sometimes fails to match — worth showing rather
                    // than hiding behind a name.
                    Text(isNamed ? "\(profile.id) · \(recordings)" : recordings)
                        .font(SettingsType.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 8)

                HStack(spacing: 6) {
                    Button(isNamed ? "Rename…" : "Name…", action: beginEditing)
                    // Another recording of somebody already here. The single
                    // most effective thing in this pane: one voiceprint is
                    // one four-second slice of a person, and recognition
                    // improves faster with more samples than with any slider.
                    if let name = profile.name {
                        Button("Improve") { improve(name) }
                            .help("Have them say a couple more sentences. This "
                                + "sharpens their profile rather than adding a second one.")
                    }
                }
                .controlSize(.small)

                Button(action: forget) {
                    Image(systemName: "trash")
                        .font(.system(size: 11))
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Forget this voice. Their id is never reused, so anything "
                    + "already recorded against it keeps meaning what it meant.")
                .frame(width: 16)
                // Revealed on hover, as on Models.
                .opacity(isHovered ? 1 : 0)
            }
        }
        .padding(.vertical, 5)
        .settingsListRowHover(isHovered && !isEditing)
        .contentShape(Rectangle())
        .onHover { isHovered = $0 }
        .accessibilityElement(children: .contain)
    }
}
