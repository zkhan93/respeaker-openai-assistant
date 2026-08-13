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

    /// The chosen animation, and the view drawing it.
    ///
    /// **The style owns the panel's size**, so switching between a bar row
    /// and a ring resizes the window rather than squashing one into the
    /// other's frame. Small either way, on purpose: this sits over whatever
    /// the user is working in, so it has to be readable at a glance and
    /// then forgettable — a panel large enough to study is a panel that is
    /// in the way.
    private var style: IndicatorStyle
    private var indicator: IndicatorView

    /// Above the Dock, clear of the very bottom edge.
    private static let bottomMargin: CGFloat = 90

    init(style: IndicatorStyle = IndicatorPreference.current()) {
        self.style = style
        self.indicator = style.makeView()
        super.init(
            contentRect: NSRect(origin: .zero, size: style.panelSize),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )

        isFloatingPanel = true
        level = .statusBar
        backgroundColor = .clear
        isOpaque = false
        ignoresMouseEvents = true
        hidesOnDeactivate = false

        // Follow the user across spaces, and stay visible over a
        // full-screen app — dictating into a full-screen editor is a
        // completely ordinary thing to do.
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]

        let container = NSView(frame: NSRect(origin: .zero, size: style.panelSize))
        container.wantsLayer = true
        container.autoresizingMask = [.width, .height]
        container.addSubview(indicator)
        contentView = container

        applyBackdrop()
        layOutIndicator()
    }

    /// The capsule, or nothing at all.
    ///
    /// When the style asks for one it is **solid black rather than a
    /// blurred HUD material**. `NSVisualEffectView` is translucent by
    /// design — it samples whatever is behind the panel, so the bars sat on
    /// a mid-grey that shifted with the wallpaper, and white-on-grey is
    /// barely a contrast at all. A fixed background means the wave has one
    /// known colour to stand against no matter what it floats over.
    ///
    /// The radial styles ask for no backdrop, and everything here has to be
    /// undone rather than merely left unset — this runs again on every
    /// restyle, so a value set for the previous style would survive into
    /// the next one.
    private func applyBackdrop() {
        guard let layer = contentView?.layer else { return }

        if style.hasBackdrop {
            layer.backgroundColor = NSColor.black.cgColor
            // Half the height: a capsule for the bar row, whose short sides
            // become full semicircles rather than merely rounded corners.
            layer.cornerRadius = style.cornerRadius
            // A black capsule on a black background — a dark terminal, a
            // full-screen editor — would be an invisible window with bars
            // floating in mid-air. The hairline keeps its shape readable
            // without being noticeable against anything lighter.
            layer.borderWidth = 1
            layer.borderColor = NSColor.white.withAlphaComponent(0.18).cgColor
            // Clipped to the capsule, which is what makes the ends round.
            layer.masksToBounds = true
        } else {
            layer.backgroundColor = NSColor.clear.cgColor
            layer.cornerRadius = 0
            layer.borderWidth = 0
            // **Unclipped on purpose.** The halo each mark draws for
            // contrast extends past its own line, and clipping to the
            // bounds would shave it off at the panel edge — a faint square
            // crop around a shape with no square in it.
            layer.masksToBounds = false
        }

        // The window shadow is computed from the content's opacity, so with
        // a transparent panel it has nothing to trace but the marks
        // themselves — and it caches, which on animating content leaves the
        // previous frame's shadow behind the current one. The halo is the
        // replacement, drawn per mark, per frame.
        hasShadow = style.hasBackdrop
        invalidateShadow()
    }

    /// Tight insets — every point given away comes straight off the
    /// animation's dynamic range, which is the thing being looked at. How
    /// tight depends on the shape, so the style supplies it.
    private func layOutIndicator() {
        guard let container = contentView else { return }
        indicator.frame = container.bounds.insetBy(
            dx: style.contentInset.width, dy: style.contentInset.height)
        indicator.autoresizingMask = [.width, .height]
    }

    // MARK: - Style

    /// Swap the animation without a relaunch.
    ///
    /// Applied live because nothing about the choice reaches the core — it
    /// is the same level events drawn differently. Rebuilding rather than
    /// reconfiguring: the styles share no state beyond the level pipeline,
    /// and a view that had to be able to become another style would be a
    /// third implementation of all three.
    func restyle(_ next: IndicatorStyle) {
        guard next != style, let container = contentView else { return }
        let wasVisible = isVisible

        indicator.stopAnimating()
        indicator.removeFromSuperview()

        style = next
        indicator = next.makeView()

        setContentSize(next.panelSize)
        container.addSubview(indicator)
        applyBackdrop()
        layOutIndicator()

        // Resizing moves the panel off centre, and a style chosen while
        // dictation is running should not leave the indicator sitting
        // askew until the next turn.
        if wasVisible {
            reposition()
            indicator.reset()
            indicator.startAnimating()
        }
    }

    /// Never key, never main — see the class note.
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    // MARK: - Lifecycle

    func show() {
        reposition()
        indicator.reset()
        indicator.startAnimating()
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
            self?.indicator.stopAnimating()
        }
    }

    func update(peak: Int, blocks: [Int]) {
        indicator.append(blocks: blocks.isEmpty ? [peak] : blocks)
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
                x: frame.midX - style.panelSize.width / 2,
                y: frame.minY + Self.bottomMargin
            )
        )
    }
}
