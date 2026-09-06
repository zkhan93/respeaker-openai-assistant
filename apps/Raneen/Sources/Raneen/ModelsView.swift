import AppKit
import SwiftUI

/// The model library: what is on this Mac, what can be fetched, and which
/// one the core will load.
///
/// **Its own pane rather than a pop-up in Transcription.** A menu listing
/// twelve models cannot show a size, cannot show a download in progress and
/// cannot offer to delete anything — and the whole point of this surface is
/// that choosing a heavier model no longer means reading a README, running a
/// shell script and then finding a hidden directory in an open panel.
///
/// The panel survives as one button at the bottom, for a model built or
/// converted somewhere else. It is an escape hatch now, not the only way in.
struct ModelsView: View {

    @ObservedObject var model: SettingsModel
    @ObservedObject var downloader: ModelDownloader

    init(model: SettingsModel) {
        self.model = model
        self.downloader = model.downloader
    }

    var body: some View {
        Group {
            if model.config.engine != .local {
                // Not an error, and not hidden either: someone who arrives
                // here after choosing a server would otherwise be tuning a
                // control with no effect.
                SettingsCallout(
                    .info,
                    "Transcription is set to a server, so none of these are in use. "
                        + "They still matter as the fallback, if that is switched on.")
            }

            SettingsCard(
                "English only",
                detailTitle: "Why these are listed apart",
                detail: """
                    An English-only model given other speech does not fail — it \
                    transliterates into English phonemes and returns confident \
                    nonsense that reads like a hallucination. Choosing one here \
                    sets the spoken language to English, because no setting can \
                    work around it. They are smaller and slightly sharper on \
                    English than the multilingual model of the same size.
                    """
            ) {
                rows(for: ModelCatalog.englishOnly)
            }

            SettingsCard(
                "Every language",
                detailTitle: "What quantised means",
                detail: """
                    These carry every language whisper supports, not only \
                    English. Most are quantised, which on these weights is \
                    close to free: \
                    large-v3 is 3.1 GB at full precision and 1.1 GB as q5_0, \
                    for a difference dictation will rarely notice. The full \
                    precision variants are here anyway — the choice belongs to \
                    whoever has the disk.
                    """
            ) {
                rows(for: ModelCatalog.multilingual)
            }

            addedCard
        }
    }

    @ViewBuilder
    private func rows(for models: [CatalogModel]) -> some View {
        ForEach(Array(models.enumerated()), id: \.element.id) { index, candidate in
            if index > 0 { SettingsDivider() }
            ModelRow(
                title: candidate.title,
                detail: candidate.detail,
                size: candidate.sizeDescription,
                isSelected: selected == candidate.filename,
                installedPath: downloader.installed[candidate.filename],
                isBundled: isBundled(candidate.filename),
                state: downloader.states[candidate.filename],
                select: { select(candidate.filename) },
                get: { downloader.start(candidate) },
                cancel: { downloader.cancel(candidate.filename) },
                remove: { model.deleteModel(candidate.filename) }
            )
        }
    }

    /// Models on disk the catalogue does not list.
    ///
    /// Always shown, even when empty, because it carries the button that
    /// adds one — and a card that appears only once you have used the feature
    /// is a card nobody finds.
    private var addedCard: some View {
        SettingsCard("Added by you") {
            if model.addedModels.isEmpty {
                SettingsEmptyState(
                    symbol: "square.stack.3d.up.badge.a",
                    "Nothing yet. Any ggml Whisper model works, including one you "
                        + "converted or quantised yourself.")
            } else {
                ForEach(Array(model.addedModels.enumerated()), id: \.element.id) { index, added in
                    if index > 0 { SettingsDivider() }
                    ModelRow(
                        title: added.name,
                        detail: added.path,
                        size: added.sizeDescription,
                        isSelected: model.config.modelPath == added.path,
                        installedPath: added.path,
                        // A file the user pointed at somewhere else on disk is
                        // theirs, not ours to delete — so no trash button
                        // unless it is in the download directory.
                        isBundled: !ModelInstall.isRemovable(path: added.path),
                        state: nil,
                        select: { model.config.modelPath = added.path },
                        get: {},
                        cancel: {},
                        remove: { model.deleteModel(added.name) }
                    )
                }
            }

            HStack {
                // Abbreviated: the absolute form is `/Users/<name>/…`, which
                // is longer, no more useful, and someone's name in a
                // screenshot.
                SettingsFootnote(
                    "Downloads go to "
                        + "\((ModelInstall.directory.path as NSString).abbreviatingWithTildeInPath).")
                Spacer(minLength: 12)
                Button("Add a Model File…") { choose() }
            }
        }
    }

    // MARK: - Selection

    /// Which filename the core will actually load.
    ///
    /// No explicit choice means the core resolves one itself, and its answer
    /// is `defaultFilename` — so that row shows as selected rather than the
    /// window showing nothing selected while dictation plainly works.
    private var selected: String {
        guard let path = model.config.modelPath else { return ModelInstall.defaultFilename }
        return (path as NSString).lastPathComponent
    }

    private func select(_ filename: String) {
        // Nothing to do if it is already the one in use. Assigning anyway
        // would replace an implicit selection with an identical explicit one
        // and light the Apply button for a change with no effect.
        guard selected != filename, let path = downloader.installed[filename] else { return }
        model.config.modelPath = path
    }

    /// Whether the copy we found is the one inside the app bundle.
    private func isBundled(_ filename: String) -> Bool {
        guard let path = downloader.installed[filename] else { return false }
        return !ModelInstall.isRemovable(path: path)
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.title = "Choose a ggml Whisper Model"
        panel.allowsOtherFileTypes = true
        panel.canChooseDirectories = false
        panel.reveal(ModelLibrary.searchPaths)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        model.config.modelPath = url.path
        model.refreshLibraries()
    }
}

extension NSOpenPanel {

    /// Open where the models actually are.
    ///
    /// **Both model caches live under `~/.cache`, which is hidden**, so a
    /// panel opening at its default location cannot reach the files our own
    /// downloader wrote — the user has to know about ⌘⇧. or ⌘⇧G to get there.
    /// Pointing the panel at the directory is the whole fix.
    ///
    /// Less load-bearing than it was, now that the whisper models have a
    /// catalogue and nobody has to visit Finder to get one. Wake words still
    /// arrive this way.
    func reveal(_ candidates: [URL]) {
        showsHiddenFiles = true
        if let directory = candidates.first(where: {
            FileManager.default.fileExists(atPath: $0.path)
        }) {
            directoryURL = directory
        }
    }
}

/// One model: what it is, what it costs, and the one thing to do about it.
///
/// The row body selects, and only when the model is already on disk —
/// downloading is the explicit button. That asymmetry is deliberate: a stray
/// click on a row should never begin a 3.1 GB transfer, and the same click on
/// an installed model is harmless.
private struct ModelRow: View {

    let title: String
    let detail: String
    let size: String
    let isSelected: Bool
    let installedPath: String?
    let isBundled: Bool
    let state: DownloadState?

    let select: () -> Void
    let get: () -> Void
    let cancel: () -> Void
    let remove: () -> Void

    @State private var isHovered = false

    private var isInstalled: Bool { installedPath != nil }

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            // Top-aligned rather than centred on the block: a mark floating
            // level with the second line looks like it belongs to the
            // description rather than to the model.
            mark.padding(.top, 2).frame(maxHeight: .infinity, alignment: .top)

            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(title).font(SettingsType.label)
                    if isBundled {
                        SettingsBadge("included")
                    }
                }
                Text(caption)
                    .font(SettingsType.caption)
                    .foregroundStyle(isFailed ? SettingsPalette.warning : .secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Spacer(minLength: 8)
            action

            // Its own column at the far right, so a size and a Get button in
            // adjacent rows end at the same edge. Reserved whether or not
            // there is a trash to draw: laid out only on hover, the rows
            // would shuffle sideways under the pointer.
            trash.frame(width: 16)
        }
        .padding(.vertical, 5)
        // Lit under the pointer only where a click selects — a row that
        // offers a download is a button's row, not a selectable one, and
        // lighting it would promise a click it does not honour.
        .settingsListRowHover(isHovered && isInstalled)
        .contentShape(Rectangle())
        .onHover { isHovered = $0 }
        // Only where a click means something. A row offering a download is
        // not a row you can select, so it must not behave like one.
        .onTapGesture { if isInstalled { select() } }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(isSelected ? [.isSelected] : [])
    }

    private var isFailed: Bool {
        if case .failed = state { return true }
        return false
    }

    /// What the row says under its name: the tradeoff normally, and the
    /// transfer while one is running — the second is the more useful of the
    /// two exactly then.
    private var caption: String {
        if let state, state.isBusy || isFailed { return state.detail }
        return detail
    }

    /// Selection, as a filled mark when chosen and a ring when merely
    /// available. Absent entirely for a model that is not here yet: an empty
    /// ring beside something you cannot select reads as a control.
    private var mark: some View {
        Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
            .font(.system(size: 13))
            .foregroundStyle(isSelected ? SettingsPalette.brand : Color.secondary.opacity(0.45))
            .opacity(isInstalled ? 1 : 0)
    }

    @ViewBuilder
    private var action: some View {
        if let state, state.isBusy {
            HStack(spacing: 8) {
                // Determinate whenever the server said how big the file is,
                // which it always does here — the indeterminate case is the
                // hash afterwards, where there is no honest fraction to show.
                // Brand-tinted for the same reason the Apply button is: at
                // the default grey on a dark card the bar is nearly invisible,
                // which is a poor showing for the one thing in the window that
                // is actually moving.
                if let fraction = state.fraction {
                    ProgressView(value: fraction)
                        .progressViewStyle(.linear)
                        .tint(SettingsPalette.brand)
                        .frame(width: 76)
                } else {
                    ProgressView()
                        .progressViewStyle(.linear)
                        .tint(SettingsPalette.brand)
                        .frame(width: 76)
                }
                Button {
                    cancel()
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .help("Stop this download")
            }
        } else if isFailed {
            Button("Retry", action: get)
                .font(SettingsType.control)
        } else if isInstalled {
            Text(size)
                .font(SettingsType.caption)
                .monospacedDigit()
                .foregroundStyle(.secondary)
        } else {
            Button(action: get) {
                Text("Get \(size)")
            }
            .font(SettingsType.control)
        }
    }

    /// Deleting, revealed on hover.
    ///
    /// Twelve permanently-visible trash icons read as a list of things to
    /// destroy rather than as a library. Absent entirely for the copy inside
    /// the app bundle and for a file added from elsewhere on disk — neither
    /// is this app's to remove.
    @ViewBuilder
    private var trash: some View {
        if isInstalled && !isBundled {
            Button {
                remove()
            } label: {
                Image(systemName: "trash")
                    .font(.system(size: 11))
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("Delete this model from disk")
            .opacity(isHovered ? 1 : 0)
        } else {
            Color.clear
        }
    }
}
