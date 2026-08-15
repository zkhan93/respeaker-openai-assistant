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
                Toggle("Identify who is speaking", isOn: $model.config.identifySpeakers)
                    .toggleStyle(.switch)

                if model.config.identifySpeakers {
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
                            + "dictation turns are under four seconds."
                    ) {
                        Picker("", selection: $model.config.speakerWindow) {
                            ForEach(SettingsStore.Limits.speakerWindowChoices, id: \.self) {
                                Text("\(Int($0)) s").tag($0)
                            }
                        }
                        .pickerStyle(.segmented)
                        .labelsHidden()
                        .frame(width: 212)
                    }

                    SettingsRow(
                        "Check again every",
                        caption: "People interrupt and hand over mid-sentence, so this "
                            + "re-checks while someone is still talking rather than "
                            + "deciding once per turn."
                    ) {
                        HStack(spacing: 8) {
                            Slider(value: $model.config.speakerInterval, in: 0.5...10, step: 0.5)
                                .frame(width: 160)
                            Text("\(model.config.speakerInterval, specifier: "%.1f") s")
                                .monospacedDigit()
                                .foregroundStyle(.secondary)
                                .frame(width: 44, alignment: .trailing)
                        }
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
                            + "what happens from now on."
                    ) {
                        HStack(spacing: 8) {
                            Slider(
                                value: $model.config.speakerThreshold,
                                in: SettingsStore.Limits.speakerThreshold,
                                step: 0.01
                            )
                            .frame(width: 160)
                            Text("\(Int(model.config.speakerThreshold * 100))%")
                                .monospacedDigit()
                                .foregroundStyle(.secondary)
                                .frame(width: 44, alignment: .trailing)
                        }
                    }

                    Text(thresholdAdvice)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
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
                        Text(emptyMessage)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    } else {
                        ForEach(model.speakers) { profile in
                            row(for: profile)
                        }
                    }

                    Divider().padding(.vertical, 2)

                    if let learning = model.learning {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("Listening for \(learning) — say a couple of sentences.")
                                .foregroundStyle(.secondary)
                            Spacer()
                            Button("Cancel") { model.cancelLearning() }
                        }
                    } else if adding {
                        HStack(spacing: 8) {
                            TextField("Name", text: $draftName)
                                .textFieldStyle(.roundedBorder)
                                .frame(maxWidth: 200)
                                .onSubmit { beginLearning() }
                            Button("Start listening") { beginLearning() }
                                .keyboardShortcut(.defaultAction)
                                .disabled(trimmedDraft.isEmpty)
                            Button("Cancel") { adding = false; draftName = "" }
                        }
                    } else {
                        Button {
                            draftName = ""
                            adding = true
                        } label: {
                            Label("Add a person…", systemImage: "plus")
                        }
                        .disabled(model.running?.identifySpeakers != true)
                        .help(model.running?.identifySpeakers == true
                            ? "Type a name, then have them speak for a few seconds."
                            : "Apply the change first — the core has to be listening.")
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
                + "seconds. Until somebody is here, every voice is reported as "
                + "unknown."
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

    @ViewBuilder
    private func row(for profile: Helper.SpeakerProfile) -> some View {
        HStack(spacing: 10) {
            // Playing the clip is the whole reason this row can be named,
            // so it takes the leading position the icon used to hold.
            // Profiles stored before clips existed have none; those keep
            // the plain icon rather than an button that would do nothing.
            if let clip = profile.clip {
                Button {
                    clips.toggle(profile.id, url: clip)
                } label: {
                    Image(
                        systemName: clips.playing == profile.id
                            ? "stop.circle.fill" : "play.circle")
                        .font(.title3)
                }
                .buttonStyle(.borderless)
                .help("Hear the recording this profile was created from.")
                .frame(width: 18)
            } else {
                Image(systemName: "person.wave.2")
                    .foregroundStyle(profile.name == nil ? .secondary : .primary)
                    .frame(width: 18)
            }

            if editing == profile.id {
                TextField("Name", text: $draftName)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 200)
                    .onSubmit { commit(profile) }
                Button("Save") { commit(profile) }
                    .keyboardShortcut(.defaultAction)
                Button("Cancel") { editing = nil }
            } else {
                VStack(alignment: .leading, spacing: 1) {
                    Text(profile.name ?? profile.id)
                        .fontWeight(profile.name == nil ? .regular : .medium)
                        .foregroundStyle(profile.name == nil ? .secondary : .primary)
                    // The sample count is the honest measure of how well
                    // this profile is known, and it is why a fresh one
                    // sometimes fails to match — worth showing rather
                    // than hiding behind a name.
                    Text(profile.name == nil
                        ? "\(profile.samples) recording\(profile.samples == 1 ? "" : "s")"
                        : "\(profile.id) · \(profile.samples) recording\(profile.samples == 1 ? "" : "s")")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button(profile.name == nil ? "Name…" : "Rename…") {
                    draftName = profile.name ?? ""
                    editing = profile.id
                }
                // Another recording of somebody already here. The single
                // most effective thing in this pane: one voiceprint is
                // one four-second slice of a person, and recognition
                // improves faster with more samples than with any slider.
                if let name = profile.name {
                    Button("Improve") { model.learnSpeaker(named: name) }
                        .help("Have them say a couple more sentences. This "
                            + "sharpens their profile rather than adding a second one.")
                }
                Button {
                    // Stop first: the core deletes the recording along with
                    // the profile, and a player holding a file that is
                    // about to vanish is a needless race.
                    if clips.playing == profile.id { clips.stop() }
                    model.forgetSpeaker(profile.id)
                } label: {
                    Image(systemName: "trash")
                }
                .help("Forget this voice. Their id is never reused, so anything "
                    + "already recorded against it keeps meaning what it meant.")
            }
        }
        .padding(.vertical, 2)
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
