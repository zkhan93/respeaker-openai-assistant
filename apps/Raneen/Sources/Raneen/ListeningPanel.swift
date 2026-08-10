import AppKit

/// The floating "I am listening" indicator.
///
/// Bottom-centre of the active screen, visible only while dictation is
/// armed. Its job is to answer, without looking away from what you are
/// typing into, two questions: is it on, and can it hear me.
///
/// **The hard requirement is that this window never takes focus.** The
/// whole product depends on the user's text cursor staying exactly where
/// it was — the moment this panel becomes key, the app being dictated
/// into resigns first responder and the transcript has nowhere to go.
/// Four separate things enforce that, and all of them are load bearing:
///
/// * `.nonactivatingPanel` in the style mask — shows without activating
///   the app.
/// * `canBecomeKey` / `canBecomeMain` overridden to `false` — a plain
///   `NSPanel` will happily become key if clicked.
/// * `ignoresMouseEvents = true` — clicks pass straight through to the
///   window underneath, so it cannot be clicked in the first place.
/// * `orderFrontRegardless()` rather than `makeKeyAndOrderFront(_:)`.
///
/// This is also the shell that ROADMAP §5c needs: revision requires
/// owning the text until it is committed, and owning text requires a
/// surface to hold it in. Nothing here does that yet, but this is where
/// it would go.
final class ListeningPanel: NSPanel {

    private let meter = ActivityMeter(frame: .zero)

    /// Small on purpose. This sits over whatever the user is working in,
    /// so it has to be readable at a glance and then forgettable — a
    /// panel large enough to study is a panel that is in the way.
    private static let size = NSSize(width: 62, height: 26)

    /// Above the Dock, clear of the very bottom edge.
    private static let bottomMargin: CGFloat = 90

    init() {
        super.init(
            contentRect: NSRect(origin: .zero, size: Self.size),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )

        isFloatingPanel = true
        level = .statusBar
        backgroundColor = .clear
        isOpaque = false
        hasShadow = true
        ignoresMouseEvents = true
        hidesOnDeactivate = false

        // Follow the user across spaces, and stay visible over a
        // full-screen app — dictating into a full-screen editor is a
        // completely ordinary thing to do.
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]

        // Solid black rather than a blurred HUD material.
        //
        // `NSVisualEffectView` is translucent by design — it samples
        // whatever is behind the panel, so the bars sat on a mid-grey
        // that shifted with the wallpaper, and white-on-grey is barely a
        // contrast at all. A fixed background means the wave has one
        // known colour to stand against no matter what it floats over.
        let container = NSView(frame: NSRect(origin: .zero, size: Self.size))
        container.wantsLayer = true
        container.layer?.backgroundColor = NSColor.black.cgColor
        // Half the height gives a capsule: the short sides become full
        // semicircles rather than merely rounded corners.
        container.layer?.cornerRadius = Self.size.height / 2
        container.layer?.masksToBounds = true
        // A black capsule on a black background — a dark terminal, a
        // full-screen editor — would be an invisible window with bars
        // floating in mid-air. The hairline keeps its shape readable
        // without being noticeable against anything lighter.
        container.layer?.borderWidth = 1
        container.layer?.borderColor = NSColor.white.withAlphaComponent(0.18).cgColor
        container.autoresizingMask = [.width, .height]

        // Tight insets, and tighter vertically than horizontally: the
        // capsule's rounded ends already supply the side margin, while
        // every point given away at the top and bottom comes straight off
        // the wave's dynamic range — which is the thing being looked at.
        meter.frame = container.bounds.insetBy(dx: 8, dy: 3)
        meter.autoresizingMask = [.width, .height]
        container.addSubview(meter)

        contentView = container
    }

    /// Never key, never main — see the class note.
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    // MARK: - Lifecycle

    func show() {
        reposition()
        meter.reset()
        meter.startAnimating()
        alphaValue = 0
        // Regardless: the app is .accessory and not active, and
        // orderFront would be ignored.
        orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.12
            animator().alphaValue = 1
        }
    }

    func hide() {
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.18
            animator().alphaValue = 0
        } completionHandler: { [weak self] in
            self?.orderOut(nil)
            self?.meter.stopAnimating()
        }
    }

    func update(peak: Int, blocks: [Int]) {
        meter.append(blocks: blocks.isEmpty ? [peak] : blocks)
    }

    /// Bottom-centre of whichever screen the pointer is on — which is
    /// the screen the user is working on. Anchoring to the main screen
    /// puts it on the wrong display in a multi-monitor setup.
    private func reposition() {
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) }
            ?? NSScreen.main
        guard let frame = screen?.visibleFrame else { return }

        setFrameOrigin(
            NSPoint(
                x: frame.midX - Self.size.width / 2,
                y: frame.minY + Self.bottomMargin
            )
        )
    }
}
