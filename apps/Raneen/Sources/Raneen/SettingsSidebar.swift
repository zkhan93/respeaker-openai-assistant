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

/// The list of sections down the left of the window.
struct SettingsSidebar: View {

    @Binding var selection: SettingsSection

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(SettingsSection.allCases) { section in
                SettingsSidebarRow(
                    section: section,
                    isSelected: section == selection,
                    select: { selection = section }
                )
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 10)
        .padding(.top, SettingsMetric.titlebarInset)
        .padding(.bottom, 10)
        .frame(width: SettingsMetric.sidebarWidth)
        .background(SettingsSidebarMaterial())
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Settings sections")
    }
}

/// One sidebar row: a tinted glyph, a name, and selection.
///
/// **The selection pill is the brand orange, not `Color.accentColor`.**
/// That is a deliberate trade against the platform convention, which is to
/// honour whatever accent the user chose in Appearance. The argument for
/// breaking it: this app's identity *is* that orange — it is the menu-bar
/// mark, it is the listening indicator, it is the app icon — and a
/// settings window tinted someone's system pink would be the only surface
/// of the product that did not look like the product. The cost is real and
/// worth naming: a user who set a system accent does not see it here.
private struct SettingsSidebarRow: View {

    let section: SettingsSection
    let isSelected: Bool
    let select: () -> Void

    @State private var isHovered = false

    var body: some View {
        Button(action: select) {
            HStack(spacing: 9) {
                glyph
                Text(section.title)
                    .font(SettingsType.control)
                    .fontWeight(isSelected ? .semibold : .regular)
                    .foregroundStyle(isSelected ? Color.white : Color.primary)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 7)
            .padding(.vertical, 5)
            .background(background)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { isHovered = $0 }
        .accessibilityAddTraits(isSelected ? [.isSelected] : [])
    }

    /// A rounded tile, filled when selected and outlined when not — so the
    /// colour means "you are here" rather than "this section is orange".
    /// Five differently-tinted tiles is what System Settings does; here
    /// there is only one brand colour to spend, and spending it on the
    /// selection is worth more than spending it on decoration.
    private var glyph: some View {
        RoundedRectangle(cornerRadius: SettingsMetric.tileRadius, style: .continuous)
            .fill(isSelected ? Color.white.opacity(0.22) : SettingsPalette.brand.opacity(0.14))
            .frame(width: SettingsMetric.glyphTile, height: SettingsMetric.glyphTile)
            .overlay(
                Image(systemName: section.symbol)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(isSelected ? Color.white : SettingsPalette.brand)
            )
    }

    @ViewBuilder
    private var background: some View {
        let shape = RoundedRectangle(cornerRadius: SettingsMetric.pillRadius, style: .continuous)
        if isSelected {
            shape.fill(SettingsPalette.pill)
        } else if isHovered {
            shape.fill(SettingsPalette.hoverFill)
        } else {
            shape.fill(Color.clear)
        }
    }
}
