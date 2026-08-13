import AppKit

/// What every listening animation has to be able to do.
///
/// Deliberately the same four calls `ListeningPanel` already made, so the
/// panel does not learn which style it is showing beyond asking the style
/// to build one.
protocol IndicatorView: NSView {
    /// Feed per-block loudness, oldest first. Main thread only.
    func append(blocks: [Int])
    /// Back to silence, with no animation — called before the panel fades in.
    func reset()
    func startAnimating()
    func stopAnimating()
}

/// Which animation the listening panel draws.
///
/// **Not part of `HelperConfig`.** Everything in that struct becomes argv
/// and needs the core restarted to take effect, which is why the settings
/// window has an Apply button at all. This changes nothing the core does —
/// it is the same level events drawn differently — so it applies the
/// instant it is chosen, and putting it in `HelperConfig` would have lit
/// "the core is still running the previous settings" for a change the core
/// never sees.
enum IndicatorStyle: String, CaseIterable, Identifiable {

    /// The original: a row of bars rising and falling with one loudness
    /// value. Still the default — it is the smallest and the calmest, and
    /// the panel sits over whatever you are typing into.
    case bars

    /// Spokes around a ring, the shape rotating slowly.
    case bloom

    /// Embers orbiting a centre, pulled inward by your voice.
    case swarm

    var id: String { rawValue }

    static let fallback = IndicatorStyle.bars

    var label: String {
        switch self {
        case .bars: return "Bars"
        case .bloom: return "Bloom"
        case .swarm: return "Swarm"
        }
    }

    var detail: String {
        switch self {
        case .bars:
            return "A row that swells from the middle. The smallest and the least distracting."
        case .bloom:
            return "Spokes around a ring, turning while it listens and reaching out as you speak."
        case .swarm:
            return "Embers orbiting a centre. Your voice pulls them in; silence lets them drift."
        }
    }

    /// **The animation sizes the panel, not the other way round.** A ring
    /// squeezed into the bar row's 62×26 capsule is an ellipse, and one
    /// drawn at the capsule's height is too small to read. The panel is
    /// small either way — it floats over the user's work, so a panel large
    /// enough to study is a panel that is in the way.
    var panelSize: NSSize {
        switch self {
        case .bars: return NSSize(width: 62, height: 26)
        case .bloom, .swarm: return NSSize(width: 74, height: 74)
        }
    }

    /// Inset from the panel edge to the drawing.
    ///
    /// Tighter vertically than horizontally for the bars: the capsule's
    /// rounded ends already supply the side margin, while every point given
    /// away at the top and bottom comes straight off the dynamic range. The
    /// radial styles are square and want the same margin all round.
    var contentInset: NSSize {
        switch self {
        case .bars: return NSSize(width: 8, height: 3)
        case .bloom, .swarm: return NSSize(width: 7, height: 7)
        }
    }

    /// Half the height: a capsule for the bar row, a circle for the radial
    /// styles, from one rule rather than two special cases.
    var cornerRadius: CGFloat { panelSize.height / 2 }

    /// Whether the shape sits on a black capsule or straight on the
    /// desktop.
    ///
    /// **The backdrop is a legibility device, not decoration**, and giving
    /// it up costs something real: brand orange on an unknown background is
    /// a coin flip — around 2.3:1 against a white document, which is below
    /// anything readable. A translucent material was tried and rejected for
    /// exactly this (it samples what is behind the panel, so the bars sat
    /// on a mid-grey that shifted with the wallpaper).
    ///
    /// The radial styles can afford it anyway, and the bar row cannot. They
    /// are drawn as thin marks with gaps between them, so a dark halo
    /// behind each one buys back the contrast without a visible box —
    /// invisible over a dark editor, and what separates the orange over a
    /// white page. The bar row is solid shapes at a small size, where the
    /// same halo reads as a smudge rather than as an edge; it keeps the
    /// capsule.
    var hasBackdrop: Bool {
        switch self {
        case .bars: return true
        case .bloom, .swarm: return false
        }
    }

    func makeView() -> IndicatorView {
        switch self {
        case .bars: return ActivityMeter(frame: .zero)
        case .bloom: return BloomMeter(frame: .zero)
        case .swarm: return SwarmMeter(frame: .zero)
        }
    }
}

/// Where the chosen style is stored.
///
/// Kept out of `SettingsStore` because that type's job is the round trip
/// between `UserDefaults` and `HelperConfig`, and this never reaches the
/// core. The key is namespaced for the same reason the others are:
/// `UserDefaults.standard` is one flat dictionary shared with AppKit's own
/// state, and an unprefixed `indicator` is asking for a collision.
enum IndicatorPreference {

    static let key = "raneen.ui.indicator"

    /// An unrecognised value is treated as absent rather than repaired in
    /// place — a plist written by a newer version should degrade to the
    /// default, not be overwritten by an older one on read.
    static func current(_ defaults: UserDefaults = .standard) -> IndicatorStyle {
        guard let raw = defaults.string(forKey: key), let style = IndicatorStyle(rawValue: raw)
        else {
            return .fallback
        }
        return style
    }

    static func save(_ style: IndicatorStyle, to defaults: UserDefaults = .standard) {
        defaults.set(style.rawValue, forKey: key)
    }
}
