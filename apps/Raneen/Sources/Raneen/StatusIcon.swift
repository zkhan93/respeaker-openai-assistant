import AppKit

/// What the menu-bar icon shows.
///
/// The resting states use the Raneen mark, so the menu bar matches the
/// app icon. Errors deliberately do not: a problem should look like a
/// problem, not like a differently-shaded logo, so those fall back to a
/// system symbol.
///
/// **Every image here is given an explicit size.** That is the whole
/// reason this file has a `menuBarHeight` constant rather than letting
/// images size themselves. A menu-bar image that relies on its intrinsic
/// size gets clipped, and — worse — clipped *inconsistently*: a MacBook
/// with a notch has a menu bar around 37pt tall while an external
/// display is nearer 24pt, so an icon that looks correct on the built-in
/// screen is sliced in half the moment the menu bar moves to a monitor.
/// 18pt is Apple's guidance and fits every configuration.
enum StatusIcon: Equatable {

    /// Launched, helper not ready yet.
    case starting
    /// Ready, waiting for the key.
    case idle
    /// Recording right now.
    case armed
    /// Something failed — a lost transcript, a dead helper.
    case error
    /// The helper is gone; nothing will work until restart.
    case stopped

    /// Fits a 24pt external menu bar and a 37pt notched one alike.
    static let menuBarHeight: CGFloat = 18

    /// Brand orange, matching the app icon and the mark itself.
    static let brandColor = NSColor(
        srgbRed: 0xF5 / 255, green: 0x8B / 255, blue: 0x2E / 255, alpha: 1
    )

    /// Map a protocol state pattern onto an icon.
    ///
    /// Unknown patterns fall back to `idle`: the helper may be newer than
    /// this app, and the `Indicator` contract (AD-9) says unrecognised
    /// patterns are ignored, not fatal.
    static func forPattern(_ pattern: String) -> StatusIcon {
        switch pattern {
        case "armed":    return .armed
        // A turn opened by a wake word or by the VAD is every bit as live as
        // a held key — `listen` is the only signal those triggers give, since
        // they have no arming layer to report. Falling through to `.idle` left
        // the menu bar claiming nothing was happening while the core was
        // recording the user.
        case "listen":   return .armed
        case "disarmed": return .idle
        case "error":    return .error
        case "off":      return .idle
        default:         return .idle
        }
    }

    /// Whether this state is drawn with the brand mark rather than a
    /// system symbol.
    var usesMark: Bool {
        switch self {
        case .idle, .armed, .starting: return true
        case .error, .stopped:         return false
        }
    }

    /// Only used when the mark asset is unavailable — running the binary
    /// outside a bundle, mainly.
    var symbolName: String {
        switch self {
        case .starting: return "mic.slash"
        case .idle:     return "mic"
        case .armed:    return "mic.fill"
        case .error:    return "exclamationmark.triangle.fill"
        case .stopped:  return "xmark.circle"
        }
    }

    /// Read aloud by VoiceOver. A menu-bar item with no description is
    /// announced as an unlabelled button, which is useless — and this is
    /// an accessibility tool.
    var accessibilityDescription: String {
        switch self {
        case .starting: return "Raneen starting"
        case .idle:     return "Raneen ready"
        case .armed:    return "Raneen recording"
        case .error:    return "Raneen error"
        case .stopped:  return "Raneen stopped"
        }
    }

    /// Render for the status bar, or nil if nothing could be loaded.
    func image() -> NSImage? {
        if usesMark, let mark = Self.markImage() {
            let image = Self.resized(mark)
            if self == .armed {
                // Non-template and brand-coloured: recording is the one
                // state worth catching the eye, and a template image
                // cannot carry colour of its own.
                let tinted = Self.tinted(image, with: Self.brandColor)
                tinted.accessibilityDescription = accessibilityDescription
                return tinted
            }
            image.isTemplate = true
            image.accessibilityDescription = accessibilityDescription
            return image
        }

        guard let symbol = NSImage(
            systemSymbolName: symbolName,
            accessibilityDescription: accessibilityDescription
        ) else { return nil }
        symbol.isTemplate = true
        return Self.resized(symbol, isTemplate: true)
    }

    // MARK: - Internals

    /// Loaded once — decoding a PNG on every state change would be
    /// wasteful for something that changes on every keypress.
    private static let cachedMark: NSImage? = {
        guard let url = Bundle.main.url(forResource: "MenuBarMark", withExtension: "png"),
              let image = NSImage(contentsOf: url) else { return nil }
        return image
    }()

    private static func markImage() -> NSImage? { cachedMark }

    /// Scale to the menu-bar height, preserving aspect. Returns a copy so
    /// the cached original is never mutated.
    private static func resized(_ image: NSImage, isTemplate: Bool = false) -> NSImage {
        let source = image.size
        guard source.height > 0 else { return image }
        let scale = menuBarHeight / source.height
        let copy = image.copy() as? NSImage ?? image
        copy.size = NSSize(width: source.width * scale, height: menuBarHeight)
        copy.isTemplate = isTemplate
        return copy
    }

    private static func tinted(_ image: NSImage, with color: NSColor) -> NSImage {
        let output = NSImage(size: image.size)
        output.lockFocus()
        image.draw(in: NSRect(origin: .zero, size: image.size))
        color.set()
        NSRect(origin: .zero, size: image.size).fill(using: .sourceAtop)
        output.unlockFocus()
        output.isTemplate = false
        return output
    }
}
