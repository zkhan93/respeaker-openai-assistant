import SwiftUI

/// The five things this window configures.
///
/// **A sidebar rather than the tab strip this replaced.** Five tabs across
/// the top of a window is the shape a settings window had in 2005, and it
/// costs more than looks: the strip gives every section the same weight,
/// truncates its own labels, and cannot say which section a warning came
/// from once the tab is not the one you are on. macOS System Settings has
/// been a sidebar since Ventura, so this is also what a Mac user now
/// expects to click.
///
/// The order is the order of a turn — what starts it, what it becomes,
/// what ends it, then the two optional extras — rather than alphabetical
/// or "most used". Someone reading down the list is reading the pipeline.
enum SettingsSection: String, CaseIterable, Identifiable {

    case dictation
    case transcription
    /// Next to Transcription rather than at the end: it is where that
    /// section's single most consequential choice actually happens, and it
    /// earned its own pane once it grew a download, a progress bar and a
    /// delete. Twelve models with sizes do not fit in a pop-up menu.
    case models
    case detection
    case wakeWord
    /// After Wake Word and before Recording: it is the other thing that
    /// listens alongside dictation without changing it, and it shares
    /// Recording's property of costing something real when switched on.
    case speakers
    case recording

    var id: String { rawValue }

    /// Which of the two lists in the sidebar this belongs to.
    ///
    /// The first four are the turn itself, in order; the last three run
    /// alongside a turn without changing it, and each costs something when
    /// switched on. Naming the split is what lets the sidebar show seven
    /// rows without reading as one undifferentiated column — and it is the
    /// same split the summaries already make, said once at the top of
    /// each group rather than implied seven times.
    var group: SettingsGroup {
        switch self {
        case .dictation, .transcription, .models, .detection: return .turn
        case .wakeWord, .speakers, .recording: return .alongside
        }
    }

    var title: String {
        switch self {
        case .dictation: return "Dictation"
        case .transcription: return "Transcription"
        case .models: return "Models"
        case .detection: return "Detection"
        case .wakeWord: return "Wake Word"
        case .speakers: return "Speakers"
        case .recording: return "Recording"
        }
    }

    /// Carried over from the tab items unchanged: they were already the
    /// right symbols, and a glyph a user has learned is not worth
    /// redesigning for its own sake.
    var symbol: String {
        switch self {
        case .dictation: return "mic"
        case .transcription: return "text.bubble"
        case .models: return "square.stack.3d.up"
        case .detection: return "waveform"
        case .wakeWord: return "ear"
        case .speakers: return "person.wave.2"
        case .recording: return "dot.radiowaves.left.and.right"
        }
    }

    /// One line under the pane title, saying what the section decides.
    ///
    /// Written in the user's terms — "a turn", "text", "speech" — because
    /// the alternative is naming the component that implements it, and
    /// nobody arrives at this window looking for the segmenter.
    var summary: String {
        switch self {
        case .dictation:
            return "How a turn starts, and what you see while it runs."
        case .transcription:
            return "Where speech becomes text, and in which language."
        case .models:
            return "The models on this Mac, and the heavier ones you can fetch."
        case .detection:
            return "What counts as speech, and where a turn ends."
        case .wakeWord:
            return "The words that can open a turn, and how keenly they listen."
        case .speakers:
            return "Who is talking, and the voices this Mac has learned."
        case .recording:
            return "Publishing audio and events to other machines."
        }
    }
}

/// The two lists in the sidebar. See `SettingsSection.group`.
enum SettingsGroup: CaseIterable {
    case turn
    case alongside

    /// Small capitals over each group, as Finder and Notes label theirs.
    /// Two words each, in the user's terms: what a turn is made of, and
    /// what runs beside one.
    var label: String {
        switch self {
        case .turn: return "The turn"
        case .alongside: return "Alongside"
        }
    }

    var sections: [SettingsSection] {
        SettingsSection.allCases.filter { $0.group == self }
    }
}

/// The list of sections down the left of the window.
struct SettingsSidebar: View {

    @Binding var selection: SettingsSection

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(SettingsGroup.allCases.enumerated()), id: \.element) { index, group in
                SettingsSidebarGroupLabel(group.label)
                    // The first label clears the traffic lights and no more;
                    // later ones carry the gap that separates the groups.
                    .padding(.top, index == 0 ? 0 : 18)
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(group.sections) { section in
                        SettingsSidebarRow(
                            section: section,
                            isSelected: section == selection,
                            select: { selection = section }
                        )
                    }
                }
            }
            Spacer(minLength: 0)
            SettingsSidebarFooter()
        }
        .padding(.horizontal, 10)
        .padding(.top, SettingsMetric.titlebarInset)
        .padding(.bottom, 12)
        .frame(width: SettingsMetric.sidebarWidth)
        .background(SettingsSidebarMaterial())
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Settings sections")
    }
}

/// A group's name, set small and quiet above its rows.
private struct SettingsSidebarGroupLabel: View {

    private let text: String

    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 10.5, weight: .semibold))
            .kerning(0.6)
            .foregroundStyle(.tertiary)
            .padding(.horizontal, 10)
            .padding(.bottom, 6)
            .accessibilityAddTraits(.isHeader)
    }
}

/// One sidebar row: a glyph, a name, and selection.
///
/// **A plain symbol, not a coloured tile.** The row this replaced put every
/// glyph in an orange square, which is the shape of a phone's settings
/// list and, seven times over in one colour, reads as decoration rather
/// than as a set of distinct places. The modern Mac sidebar — Finder,
/// Mail, System Settings' own sub-panes — draws its symbols bare and lets
/// the selection carry the colour.
///
/// **Selection is a translucent fill with the brand on the glyph, not a
/// saturated orange block.** Two reasons. A filled block with white text
/// is the loudest thing in a window that otherwise speaks in hairlines and
/// footnotes, and it was fighting the pane header for attention. And a
/// translucent fill over the sidebar's vibrancy is what every macOS
/// sidebar does now; the brand orange still says "this is Raneen", on the
/// symbol and nowhere else. Still not `Color.accentColor`: this app's
/// identity is that orange — the menu-bar mark, the indicator, the icon —
/// and a system-pink glyph would be the one surface that did not look
/// like the product. A user who set a system accent does not see it here,
/// and that cost is deliberate.
private struct SettingsSidebarRow: View {

    let section: SettingsSection
    let isSelected: Bool
    let select: () -> Void

    @State private var isHovered = false

    var body: some View {
        Button(action: select) {
            HStack(spacing: 10) {
                Image(systemName: section.symbol)
                    .font(.system(size: 13, weight: isSelected ? .semibold : .medium))
                    .foregroundStyle(isSelected ? SettingsPalette.brand : Color.secondary)
                    // A fixed column, so a wide symbol and a narrow one
                    // leave the titles on the same vertical line.
                    .frame(width: SettingsMetric.glyphTile, height: SettingsMetric.glyphTile)
                Text(section.title)
                    .font(SettingsType.control)
                    .fontWeight(isSelected ? .semibold : .regular)
                    .foregroundStyle(.primary)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .background(background)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { isHovered = $0 }
        .accessibilityAddTraits(isSelected ? [.isSelected] : [])
    }

    @ViewBuilder
    private var background: some View {
        let shape = RoundedRectangle(cornerRadius: SettingsMetric.pillRadius, style: .continuous)
        if isSelected {
            shape.fill(SettingsPalette.selection)
        } else if isHovered {
            shape.fill(SettingsPalette.hoverFill)
        } else {
            shape.fill(Color.clear)
        }
    }
}

/// The app signing its own window.
///
/// The window title is hidden and the pane header names the section, so
/// nothing else in the window says which application this is. The mark
/// and the version at the foot of the sidebar do — quietly, in the
/// secondary colour, because they are identity rather than navigation.
/// The version is here rather than in an About box the app does not have:
/// a menu-bar app with no Dock icon has nowhere else to put it, and "which
/// build is this" is the first question in every bug report.
private struct SettingsSidebarFooter: View {

    /// Read once, and only from the app's own bundle. Under `swift test`
    /// `Bundle.main` is the xctest runner, which has a perfectly valid
    /// version of its own — and "16.0" in a README screenshot was the first
    /// thing this footer shipped. Outside the bundle the footer shows the
    /// name alone rather than a number that belongs to something else.
    private static let version: String? = {
        guard Bundle.main.bundleIdentifier == "com.nexuscraftlabs.raneen" else { return nil }
        return Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String
    }()

    private static let markHeight: CGFloat = 14

    var body: some View {
        HStack(spacing: 7) {
            mark
                .frame(width: Self.markHeight, height: Self.markHeight)
            Text("Raneen")
                .font(.system(size: 11, weight: .semibold))
            if let version = Self.version {
                Text(version)
                    .font(.system(size: 11))
                    .monospacedDigit()
            }
            Spacer(minLength: 0)
        }
        .foregroundStyle(.secondary)
        .padding(.horizontal, 7)
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
    }

    /// The real mark inside the bundle, a symbol outside it — the same
    /// fallback the menu bar makes, for the same reason.
    @ViewBuilder
    private var mark: some View {
        if let image = StatusIcon.brandMark(height: Self.markHeight) {
            Image(nsImage: image)
                .renderingMode(.template)
                .resizable()
                .scaledToFit()
                .foregroundStyle(SettingsPalette.brand)
        } else {
            Image(systemName: "waveform.circle.fill")
                .font(.system(size: 12))
                .foregroundStyle(SettingsPalette.brand)
        }
    }
}
