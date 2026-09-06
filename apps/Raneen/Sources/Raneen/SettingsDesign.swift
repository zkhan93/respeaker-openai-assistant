import AppKit
import SwiftUI

/// The vocabulary the settings window is built from.
///
/// **Why a vocabulary rather than twenty bespoke stacks.** The window it
/// replaced expressed every control differently — `.callout` here,
/// `.caption` there, an `HStack` with a `Spacer` for one value and a
/// `LabeledContent` for the next — and the result read as a form generator
/// rather than as a designed surface. Nothing was wrong with any single
/// line; there was simply no shared answer to "how wide is a control" or
/// "how loud is explanatory prose", so each answer was invented again.
///
/// Everything here is layout and colour only. No file in this group knows
/// what a wake word is, which is what lets the sections be re-ordered or
/// re-grouped without touching a pixel of styling.
///
/// **Deployment target is macOS 13**, so nothing here reaches for the 14+
/// conveniences that would shorten it — no `ContentUnavailableView`, no
/// `.accessoryBar` button style, no `Color.mix`. Dynamic colours come from
/// `NSColor(name:dynamicProvider:)` instead of `@Environment(\.colorScheme)`
/// branches: an `NSColor` re-resolves itself when the appearance changes,
/// while a colour chosen in `body` only re-resolves if SwiftUI happens to
/// re-evaluate that view.

// MARK: - Metrics

/// Every measurement in the window, in one place.
///
/// The point of collecting them is the right-hand edge. Controls are
/// right-aligned to the card's inner edge and given fixed widths from this
/// table, so a stepper, a slider and a pop-up in three different cards
/// line up in a column — which is the single change that makes the window
/// look composed rather than assembled. Labels take whatever is left,
/// so a long one wraps instead of shoving the controls out of alignment.
enum SettingsMetric {

    /// Wider than the 580×470 it replaced because a sidebar costs 190pt and
    /// the prose still needs a readable measure in what is left.
    ///
    /// The height is measured rather than chosen. It is what Detection and
    /// Transcription — the two tallest panes — need with every disclosure
    /// closed; anything less and the last card arrives half under the action
    /// bar, which reads as a clipped window rather than a scrollable one.
    static let windowSize = CGSize(width: 700, height: 640)

    static let sidebarWidth: CGFloat = 190

    /// Clearance for the traffic lights. The window uses
    /// `.fullSizeContentView`, so the content — not the frame — has to
    /// leave room for them.
    static let titlebarInset: CGFloat = 34

    static let gutter: CGFloat = 22
    static let cardSpacing: CGFloat = 14
    static let cardPadding: CGFloat = 15
    static let rowSpacing: CGFloat = 13
    static let calloutPadding: CGFloat = 10

    /// Twelve rather than the ten it was: the platform's own grouped
    /// surfaces have rounded further with each release, and at ten the
    /// cards read as a form from the previous decade next to them.
    static let cardRadius: CGFloat = 12
    static let calloutRadius: CGFloat = 8
    static let pillRadius: CGFloat = 7
    /// Hover on a list row inside a card — a model, a person, a wake word.
    static let listRowRadius: CGFloat = 6
    /// How far a list row's hover fill reaches past its content, so the
    /// fill has some air around the text without the text itself moving.
    static let listRowInset: CGFloat = 8

    /// Trailing control widths. Fixed rather than intrinsic so that a
    /// pop-up whose selection changes, or a slider next to a stepper, does
    /// not move the right-hand edge of the window's control column.
    static let sliderWidth: CGFloat = 130
    static let pickerWidth: CGFloat = 210
    static let fieldWidth: CGFloat = 220

    /// The number a row displays, right-aligned. Wide enough for the longest
    /// of them (`240 ms`) so a value does not shift its own label as it
    /// changes.
    static let valueColumn: CGFloat = 60

    static let glyphTile: CGFloat = 22
}

// MARK: - Palette

/// The window's colours.
///
/// Two rules. Surfaces come from `NSColor` semantic colours or from an
/// alpha over them, never from a literal grey, so the window follows the
/// system appearance including increased contrast. And the accent is the
/// brand orange the menu-bar mark and the listening indicator already use
/// — one colour for the product, not a second one invented for Settings.
enum SettingsPalette {

    /// Shared with `StatusIcon`, deliberately: the window, the menu bar and
    /// the indicator should be visibly the same application.
    static let brand = Color(nsColor: StatusIcon.brandColor)

    /// The selected sidebar row.
    ///
    /// A translucent fill, as every Mac sidebar now draws it, rather than
    /// the saturated orange gradient this replaced. That gradient was
    /// darkened to `#C97226`→`#98561D` so white text on it reached 4.5:1,
    /// and it worked — but it was the loudest object in a window that
    /// otherwise speaks in hairlines, and it read as a decade-old
    /// selection style beside the platform's own. The brand now sits on
    /// the selected glyph alone; see `SettingsSidebarRow` for the trade.
    /// Stronger than `hoverFill`, so a hovered row and the selected row
    /// are never confused for each other.
    static let selection = Color(
        nsColor: dynamic(
            light: NSColor(white: 0, alpha: 0.09), dark: NSColor(white: 1, alpha: 0.12)))

    /// The pane behind the cards.
    static let pane = Color(nsColor: .windowBackgroundColor)

    /// A card.
    ///
    /// White on the light pane, and a *lighter* wash on the dark one — the
    /// card has to read as lifted off the page in both, and in dark mode
    /// `controlBackgroundColor` is darker than the window, which reads as a
    /// hole instead.
    static let card = Color(
        nsColor: dynamic(light: .white, dark: NSColor(white: 1, alpha: 0.055)))

    /// The hairline round a card. Doing most of the work a shadow would
    /// otherwise do: a shadow under fifteen cards is mud, an edge is an edge.
    static let cardBorder = Color(
        nsColor: dynamic(
            light: NSColor(white: 0, alpha: 0.08), dark: NSColor(white: 1, alpha: 0.09)))

    /// The small amount of lift a card is allowed.
    ///
    /// Light mode only, and faint — a white card on a near-white pane is
    /// otherwise held up by its hairline alone, which is flat in the way a
    /// wireframe is flat. In dark mode the card is already a lighter wash
    /// on the pane, and a shadow under it would be black on black, so it
    /// resolves to clear rather than to a smaller alpha.
    static let cardShadow = Color(
        nsColor: dynamic(light: NSColor(white: 0, alpha: 0.05), dark: .clear))

    /// Separator inside a card, for lists of like things.
    static let hairline = Color(nsColor: .separatorColor)

    /// The unselected sidebar glyph tile, and other quiet fills.
    static let quietFill = Color(
        nsColor: dynamic(
            light: NSColor(white: 0, alpha: 0.05), dark: NSColor(white: 1, alpha: 0.07)))

    /// Hover on a sidebar row. Faint on purpose — it answers "is this
    /// clickable" and must not compete with the selection.
    static let hoverFill = Color(
        nsColor: dynamic(
            light: NSColor(white: 0, alpha: 0.045), dark: NSColor(white: 1, alpha: 0.06)))

    /// Behind the listening-indicator preview. Two of the three styles
    /// draw straight onto the desktop with no box of their own, so the
    /// preview needs a surface dark enough to be honest about them.
    static let stage = LinearGradient(
        colors: [Color(white: 0.14), Color(white: 0.07)],
        startPoint: .top,
        endPoint: .bottom
    )

    static let warning = Color(nsColor: .systemOrange)
    static let neutral = Color(nsColor: .secondaryLabelColor)

    /// The one filled button in the window.
    ///
    /// Brand orange, but held a step darker than the mark's `#F58B2E`
    /// because white 13pt semibold sits on it: the mark's own orange is
    /// about 2.3:1 against white, and this pair is 3.1:1 at the top and
    /// 3.9:1 at the bottom, which is where the platform's own accent
    /// buttons land. Written out rather than derived with
    /// `blended(withFraction:of:)` — that returns an optional and a `??`
    /// fallback would silently ship a different orange.
    static let primaryAction = LinearGradient(
        colors: [rgb(0xE2, 0x76, 0x22), rgb(0xBC, 0x5F, 0x19)],
        startPoint: .top,
        endPoint: .bottom
    )

    /// sRGB from the hex a designer reads, for the two literals above.
    private static func rgb(_ red: Int, _ green: Int, _ blue: Int) -> Color {
        Color(.sRGB, red: Double(red) / 255, green: Double(green) / 255, blue: Double(blue) / 255)
    }

    /// An `NSColor` that re-resolves per appearance, rather than a SwiftUI
    /// colour picked from `colorScheme` in `body`. The difference shows on
    /// a live appearance switch: this one follows immediately, and a value
    /// captured in `body` only follows if that view is re-evaluated.
    private static func dynamic(light: NSColor, dark: NSColor) -> NSColor {
        NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
        }
    }
}

// MARK: - Type scale

/// Four sizes, and nothing else.
///
/// Expressed as text styles rather than point sizes so the window follows
/// the system text size: on macOS that is `.title2` 17, `.headline` 13
/// semibold, `.body` 13, `.subheadline` 11. The rule the old window broke
/// is the last one — prose was set at `.callout`, one step off `.body`,
/// which made a four-line explanation look as important as the control it
/// explained. Two steps down, and secondary, it reads as a footnote.
enum SettingsType {
    static let paneTitle = Font.system(.title2).weight(.semibold)
    static let paneSummary = Font.system(.callout)
    static let cardTitle = Font.system(.headline)
    static let label = Font.system(.body)
    static let value = Font.system(.body)
    static let caption = Font.system(.subheadline)
    static let prose = Font.system(.subheadline)
    /// Sidebar rows and the disclosure toggle — a hair tighter than a row
    /// label, because both are navigation rather than content.
    static let control = Font.system(.callout)
}

// MARK: - Pane header

/// The name of the section you are looking at, and what it decides.
///
/// The window title is hidden (`titleVisibility = .hidden`), so this *is*
/// the title. It earns the space it takes: a settings pane that opens with
/// a control and no statement of scope makes you read the controls to find
/// out whether you are in the right place.
struct SettingsPaneHeader: View {

    let title: String
    let summary: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(SettingsType.paneTitle)
            Text(summary).font(SettingsType.paneSummary).foregroundStyle(.secondary)
        }
        .padding(.bottom, 4)
    }
}

// MARK: - Card

/// A group of related controls on a raised surface.
///
/// The hierarchy is the whole point: a title in semibold, controls
/// right-aligned in a column, and the explanation folded away under a
/// disclosure so it is available without being loud. In the window this
/// replaced, a section title, its controls and four lines of rationale
/// were within one step of each other in size and all in the same column,
/// so nothing led.
struct SettingsCard<Accessory: View, Content: View>: View {

    private let title: String
    private let detailTitle: String
    private let detail: String?
    private let accessory: Accessory
    private let content: Content

    /// Collapsed by default, per card. Not one flag for the window: the
    /// state belongs to the thing that opens, and a shared one would open
    /// five explanations at once.
    @State private var showsDetail = false

    init(
        _ title: String,
        detailTitle: String = "Why this is here",
        detail: String? = nil,
        @ViewBuilder accessory: () -> Accessory,
        @ViewBuilder content: () -> Content
    ) {
        self.title = title
        self.detailTitle = detailTitle
        self.detail = detail
        self.accessory = accessory()
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: SettingsMetric.rowSpacing) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(title).font(SettingsType.cardTitle)
                Spacer(minLength: 8)
                accessory
            }

            content

            if let detail {
                SettingsDisclosure(title: detailTitle, text: detail, isExpanded: $showsDetail)
            }
        }
        .padding(SettingsMetric.cardPadding)
        .frame(maxWidth: .infinity, alignment: .leading)
        // The shadow is on the shape, not on the card: a `.shadow` on the
        // whole view would also shadow every line of text inside it.
        .background(
            RoundedRectangle(cornerRadius: SettingsMetric.cardRadius, style: .continuous)
                .fill(SettingsPalette.card)
                .shadow(color: SettingsPalette.cardShadow, radius: 10, y: 3)
        )
        .overlay(
            RoundedRectangle(cornerRadius: SettingsMetric.cardRadius, style: .continuous)
                .strokeBorder(SettingsPalette.cardBorder, lineWidth: 1)
        )
    }
}

extension SettingsCard where Accessory == EmptyView {

    init(
        _ title: String,
        detailTitle: String = "Why this is here",
        detail: String? = nil,
        @ViewBuilder content: () -> Content
    ) {
        self.init(
            title, detailTitle: detailTitle, detail: detail, accessory: { EmptyView() },
            content: content)
    }
}

// MARK: - Row

/// One setting: what it is called, what it is set to, and the control.
///
/// Two rules, and they are what make a stack of these read as a table
/// rather than as a pile. Controls are right-aligned to the same edge, so
/// a pop-up, a slider and a switch in different cards end in one column.
/// And the number sits immediately to the left of the control it belongs
/// to, at `valueColumn` wide — separate from the control rather than
/// pushed inside its label, because the number is the thing being read.
/// The old window put it inside the label, where it landed wherever each
/// control's intrinsic width left it.
///
/// `caption` is for the same setting said in the other unit — frames under
/// milliseconds — and is a step down in size, so a row states its value
/// once loudly and once quietly instead of twice at the same weight.
struct SettingsRow<Control: View>: View {

    private let label: String
    private let caption: String?
    private let value: String?
    private let controlWidth: CGFloat?
    private let control: Control

    /// - Parameter controlWidth: `nil` lets the control size itself, which
    ///   is right for a switch or a button; a width is right for anything
    ///   that has to align with the row above it.
    init(
        _ label: String,
        caption: String? = nil,
        value: String? = nil,
        controlWidth: CGFloat? = nil,
        @ViewBuilder control: () -> Control
    ) {
        self.label = label
        self.caption = caption
        self.value = value
        self.controlWidth = controlWidth
        self.control = control()
    }

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 1) {
                Text(label).font(SettingsType.label)
                if let caption {
                    Text(caption).font(SettingsType.caption).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 12)
            if let value {
                Text(value)
                    .font(SettingsType.value)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                    .frame(width: SettingsMetric.valueColumn, alignment: .trailing)
            }
            control
                .labelsHidden()
                .modifier(FixedWidth(controlWidth))
        }
    }
}

/// Applying `.frame(width:)` only when there is a width, without an
/// `if` in the view tree — a branch there would give the control a new
/// identity when the width appeared or vanished and lose its focus.
private struct FixedWidth: ViewModifier {

    let width: CGFloat?

    init(_ width: CGFloat?) { self.width = width }

    func body(content: Content) -> some View {
        content.frame(width: width, alignment: .trailing)
    }
}

/// Between like things inside a card — the listed wake words, mainly.
/// Inset so it separates the rows rather than cutting the card in half.
struct SettingsDivider: View {
    var body: some View {
        Rectangle()
            .fill(SettingsPalette.hairline)
            .frame(height: 1)
            .padding(.leading, 2)
    }
}

// MARK: - Switch

/// An on/off setting, drawn the same way everywhere.
///
/// A switch rather than the checkbox `Toggle` defaults to on macOS: every
/// one of these turns a capability on, which is what a switch means, and it
/// is what every settings surface on the platform now uses. Tinted with the
/// brand so the one saturated control on a card is not a different colour
/// from the sidebar and the Apply button. Before this existed one pane
/// tinted its switch and another did not, which is exactly the kind of
/// drift a shared vocabulary is for.
struct SettingsSwitch: View {

    @Binding var isOn: Bool

    var body: some View {
        Toggle("", isOn: $isOn)
            .toggleStyle(.switch)
            .tint(SettingsPalette.brand)
            .labelsHidden()
    }
}

// MARK: - Badge

/// A short fact attached to a name — "included", "recommended".
///
/// A capsule in the brand tint at caption size: small enough that it
/// annotates the thing beside it rather than competing with it, coloured
/// so it is found by scanning. `quiet` is for a fact that is merely true
/// rather than good news, and uses the neutral fill.
struct SettingsBadge: View {

    enum Style {
        /// Good news, or the brand: "included", "recommended".
        case brand
        /// Merely true: "unnamed".
        case quiet
        /// A state the user should notice: "always open".
        case warning

        var foreground: Color {
            switch self {
            case .brand: return SettingsPalette.brand
            case .quiet: return Color.secondary
            case .warning: return SettingsPalette.warning
            }
        }

        var fill: Color {
            switch self {
            case .brand: return SettingsPalette.brand.opacity(0.12)
            case .quiet: return SettingsPalette.quietFill
            case .warning: return SettingsPalette.warning.opacity(0.14)
            }
        }
    }

    private let text: String
    private let style: Style

    init(_ text: String, style: Style = .brand) {
        self.text = text
        self.style = style
    }

    init(_ text: String, quiet: Bool) {
        self.init(text, style: quiet ? .quiet : .brand)
    }

    var body: some View {
        Text(text)
            .font(.system(size: 10, weight: .medium))
            .foregroundStyle(style.foreground)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(style.fill))
            .lineLimit(1)
            .fixedSize()
    }
}

// MARK: - Empty state

/// A list with nothing in it yet, saying so on purpose.
///
/// A glyph and a sentence, centred, at footnote weight. A bare "None." in
/// the middle of a card reads as a missing feature; this reads as a place
/// where something will be, and says what to do about it. Hand-built
/// because `ContentUnavailableView` is macOS 14 and this app runs on 13.
struct SettingsEmptyState: View {

    private let symbol: String
    private let text: String

    init(symbol: String, _ text: String) {
        self.symbol = symbol
        self.text = text
    }

    var body: some View {
        VStack(spacing: 6) {
            Image(systemName: symbol)
                .font(.system(size: 22, weight: .light))
                .foregroundStyle(.tertiary)
            Text(text)
                .font(SettingsType.prose)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .lineSpacing(2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 14)
    }
}

// MARK: - List row hover

/// The faint fill under a list row while the pointer is over it.
///
/// Rows inside a card — models, people, wake words — are clickable, and
/// nothing about a line of text says so. A hover fill is the cheapest
/// honest answer: it appears only under the pointer and only where a click
/// does something. The fill reaches `listRowInset` past the content on each
/// side and is pulled back by the same amount, so the text does not move
/// when it appears.
struct SettingsListRowHover: ViewModifier {

    let isActive: Bool

    func body(content: Content) -> some View {
        content
            .padding(.horizontal, SettingsMetric.listRowInset)
            .background(
                RoundedRectangle(cornerRadius: SettingsMetric.listRowRadius, style: .continuous)
                    .fill(isActive ? SettingsPalette.hoverFill : Color.clear)
            )
            .padding(.horizontal, -SettingsMetric.listRowInset)
    }
}

extension View {
    /// See `SettingsListRowHover`.
    func settingsListRowHover(_ isActive: Bool) -> some View {
        modifier(SettingsListRowHover(isActive: isActive))
    }
}

// MARK: - Prose

/// Explanatory text, at footnote weight.
///
/// Everything the window says about *why* is set through this, so prose is
/// visibly subordinate to labels everywhere rather than in most places.
struct SettingsFootnote: View {

    private let text: String

    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text)
            .font(SettingsType.prose)
            .foregroundStyle(.secondary)
            .lineSpacing(2)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The paragraph a card keeps folded away.
///
/// A disclosure rather than a popover, and rather than the always-visible
/// footers this replaced. The prose documents behaviour that cost real
/// debugging and none of it may be lost — but a window that shows all of
/// it at once is a wall of grey text nobody reads, which loses it just as
/// effectively. A popover was the other candidate and was rejected: it
/// dismisses on the next click, so it cannot be read *beside* the control
/// it describes, which is exactly how these paragraphs are used.
struct SettingsDisclosure: View {

    let title: String
    let text: String
    @Binding var isExpanded: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                // Short: this is a reveal, not a transition. Anything
                // slower and the click feels unacknowledged.
                withAnimation(.easeOut(duration: 0.16)) { isExpanded.toggle() }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .semibold))
                        .rotationEffect(.degrees(isExpanded ? 90 : 0))
                    Text(title)
                }
                .font(SettingsType.control)
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(isExpanded ? "Hide the explanation" : "Show the explanation")

            if isExpanded {
                SettingsFootnote(text)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Callout

/// A state the window wants read: a warning, a security note, a caution.
///
/// Boxed and tinted because the alternative is what was there before — an
/// orange `Label` in a form row, which reads as a log line that escaped
/// into the UI. A designed state also survives being scanned: the tint
/// says "something here is different" before any word is read.
struct SettingsCallout: View {

    enum Kind {
        /// Something is set up in a way that will not work.
        case warning
        /// A privacy or exposure consequence of this choice.
        case security
        /// Context that changes how the control should be used.
        case info

        var symbol: String {
            switch self {
            case .warning: return "exclamationmark.triangle.fill"
            case .security: return "exclamationmark.shield.fill"
            case .info: return "info.circle.fill"
            }
        }

        var tint: Color {
            switch self {
            case .warning, .security: return SettingsPalette.warning
            case .info: return SettingsPalette.neutral
            }
        }
    }

    private let kind: Kind
    private let text: String

    init(_ kind: Kind, _ text: String) {
        self.kind = kind
        self.text = text
    }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: kind.symbol)
                .font(.system(size: 11))
                .foregroundStyle(kind.tint)
                // Optically on the first line of text rather than on its
                // baseline: `.firstTextBaseline` sits a filled glyph low
                // enough to look dropped.
                .padding(.top, 1)
            Text(text)
                .font(SettingsType.prose)
                .foregroundStyle(.primary)
                .lineSpacing(2)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(SettingsMetric.calloutPadding)
        .background(
            kind.tint.opacity(0.10),
            in: RoundedRectangle(cornerRadius: SettingsMetric.calloutRadius, style: .continuous)
        )
        .overlay(
            RoundedRectangle(cornerRadius: SettingsMetric.calloutRadius, style: .continuous)
                .strokeBorder(kind.tint.opacity(0.22), lineWidth: 1)
        )
    }
}

// MARK: - Option

/// One of a few named choices, as a row you click.
///
/// Used where a radio group would go and a pop-up would not — when the
/// choices need to sit beside a preview of what they do. It keeps the
/// binding a plain assignment, so nothing about the control's behaviour
/// changes; only its shape does.
struct SettingsOption: View {

    let title: String
    let isSelected: Bool
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            HStack(spacing: 6) {
                Text(title).font(SettingsType.label)
                Spacer(minLength: 4)
                Image(systemName: "checkmark")
                    .font(.system(size: 10, weight: .bold))
                    .opacity(isSelected ? 1 : 0)
            }
            .foregroundStyle(isSelected ? SettingsPalette.brand : Color.primary)
            .padding(.horizontal, 9)
            .padding(.vertical, 6)
            .background(
                RoundedRectangle(cornerRadius: SettingsMetric.pillRadius, style: .continuous)
                    .fill(isSelected ? SettingsPalette.brand.opacity(0.12) : SettingsPalette.quietFill)
            )
            .overlay(
                RoundedRectangle(cornerRadius: SettingsMetric.pillRadius, style: .continuous)
                    .strokeBorder(
                        isSelected ? SettingsPalette.brand.opacity(0.55) : Color.clear,
                        lineWidth: 1)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(isSelected ? [.isSelected] : [])
    }
}

// MARK: - Stage

/// A dark surface for something that normally floats over the desktop.
///
/// Dark rather than a checkerboard or a screenshot: the listening
/// indicator's own contrast rules (`IndicatorStyle.hasBackdrop`) assume an
/// unknown background, and a light stage would flatter the one style that
/// carries its own capsule while hiding the two that do not.
struct SettingsStage<Content: View>: View {

    private let content: Content

    init(@ViewBuilder content: () -> Content) { self.content = content() }

    var body: some View {
        content
            .background(
                SettingsPalette.stage,
                in: RoundedRectangle(cornerRadius: SettingsMetric.cardRadius, style: .continuous)
            )
            .overlay(
                RoundedRectangle(cornerRadius: SettingsMetric.cardRadius, style: .continuous)
                    .strokeBorder(Color.white.opacity(0.10), lineWidth: 1)
            )
    }
}

// MARK: - Action bar

/// Whether the running core matches what is on screen.
///
/// Two named states rather than a lit or unlit button alone: "the core is
/// still running the previous settings" is the fact the user needs, and a
/// disabled button is not a sentence. Top-level rather than nested inside
/// `SettingsActionBar` because that view is generic over its actions, and a
/// type nested in a generic cannot be named without repeating the generic
/// argument at every call site.
enum SettingsStatus {

    /// Edited, not applied.
    case pending(String)
    /// What is on screen is what is running.
    case settled(String)

    var text: String {
        switch self {
        case .pending(let text), .settled(let text): return text
        }
    }

    var tint: Color {
        switch self {
        case .pending: return SettingsPalette.warning
        case .settled: return Color(nsColor: .systemGreen)
        }
    }
}

/// The window's one action, and whether it is needed.
///
/// The status text is on the left because it is the *reason* the button is
/// lit, and reading left to right reaches the reason before the action.
struct SettingsActionBar<Actions: View>: View {

    private let status: SettingsStatus?
    private let actions: Actions

    init(status: SettingsStatus?, @ViewBuilder actions: () -> Actions) {
        self.status = status
        self.actions = actions()
    }

    var body: some View {
        HStack(spacing: 8) {
            if let status {
                Circle()
                    .fill(status.tint)
                    .frame(width: 7, height: 7)
                Text(status.text)
                    .font(SettingsType.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 12)
            actions
        }
        .padding(.horizontal, SettingsMetric.gutter)
        .padding(.vertical, 12)
        // The pane's own colour with a hairline above, not the `.bar`
        // material it was. That material is a toolbar's — a lighter grey
        // strip that read as a second piece of chrome under the content,
        // and with the pane and the sidebar already two materials, a third
        // was one too many. Now the bar recedes and the button is the only
        // object on it.
        .background(SettingsPalette.pane)
        .overlay(alignment: .top) {
            Rectangle().fill(SettingsPalette.hairline).frame(height: 1)
        }
    }
}

// MARK: - Primary button

/// The window's one filled button.
///
/// A capsule in the brand gradient, with white semibold text — the shape
/// every current Mac surface gives its single confirming action, and a
/// deliberate step away from `.borderedProminent`, whose disabled state is
/// a grey slab that looks like a control from a system several versions
/// back. Disabled, this is a quiet capsule with secondary text: still
/// legibly a button, so the eye knows where Apply will appear, but with no
/// weight until there is something to apply.
struct SettingsPrimaryButtonStyle: ButtonStyle {

    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .semibold))
            .foregroundStyle(isEnabled ? Color.white : Color.secondary)
            .padding(.horizontal, 16)
            .padding(.vertical, 7)
            .background(
                Capsule().fill(
                    isEnabled
                        ? AnyShapeStyle(SettingsPalette.primaryAction)
                        : AnyShapeStyle(SettingsPalette.quietFill))
            )
            // A hairline of light along the top edge, which is what gives
            // a filled capsule its slight convexity instead of a flat fill.
            .overlay(
                Capsule().strokeBorder(
                    LinearGradient(
                        colors: [Color.white.opacity(isEnabled ? 0.28 : 0), Color.clear],
                        startPoint: .top, endPoint: .center),
                    lineWidth: 1)
            )
            .shadow(
                color: SettingsPalette.brand.opacity(isEnabled ? 0.28 : 0),
                radius: 6, y: 2)
            .opacity(configuration.isPressed ? 0.85 : 1)
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
            .contentShape(Capsule())
    }
}

// MARK: - Sidebar material

/// The vibrancy behind the sidebar.
///
/// `NSVisualEffectView` rather than SwiftUI's `Material`: a SwiftUI
/// material blurs only what is inside the window, so over an opaque pane
/// it resolves to a flat grey. `.behindWindow` samples the desktop, which
/// is what makes a macOS sidebar look like a sidebar.
struct SettingsSidebarMaterial: NSViewRepresentable {

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .sidebar
        view.blendingMode = .behindWindow
        // Desaturates with the window, so an inactive Settings window does
        // not sit there looking like the focused one.
        view.state = .followsWindowActiveState
        return view
    }

    func updateNSView(_ view: NSVisualEffectView, context: Context) {}
}

// MARK: - Formatting

/// Turning the core's units into something a person reads.
///
/// Split out of the view because the choice being made is editorial, not
/// visual, and because it is the one part of this file worth a test. The
/// old window showed `2.0 s (25)` in a single string; the value and the
/// frame count are now two pieces so the row can set them at two weights
/// — the time as the value, the frames as a caption under the label.
enum SettingsFormat {

    /// Fixed by the audio contract: PCM16, 16 kHz, 1280 samples.
    static let frameMilliseconds = 80

    /// The value shown beside the control.
    static func duration(frames: Int) -> String {
        guard frames > 0 else { return "none" }
        let ms = frames * frameMilliseconds
        return ms >= 1000 ? String(format: "%.1f s", Double(ms) / 1000) : "\(ms) ms"
    }

    /// The caption under the label. Frames are what the core counts, and
    /// dropping them would leave nothing connecting a setting to the log
    /// line or the flag that carries it.
    static func frameCount(_ frames: Int) -> String {
        switch frames {
        case 0: return "no frames"
        case 1: return "1 frame of \(frameMilliseconds) ms"
        default: return "\(frames) frames of \(frameMilliseconds) ms"
        }
    }

    static func seconds(_ value: Double) -> String {
        "\(Int(value)) s"
    }

    /// `0` is the gate being off, and saying so is the difference between
    /// "no words are discarded" and "words below 0.00 are discarded".
    static func confidence(_ value: Double) -> String {
        value == 0 ? "off" : String(format: "%.2f", value)
    }

    static func threshold(_ value: Double) -> String {
        String(format: "%.2f", value)
    }
}
