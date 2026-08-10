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

    private let waveform = WaveformView(frame: .zero)

    /// Small on purpose. This sits over whatever the user is working in,
    /// so it has to be readable at a glance and then forgettable — a
    /// panel large enough to study is a panel that is in the way.
    private static let size = NSSize(width: 96, height: 26)

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

        let container = NSVisualEffectView(frame: NSRect(origin: .zero, size: Self.size))
        container.material = .hudWindow
        container.blendingMode = .behindWindow
        container.state = .active
        container.wantsLayer = true
        // Half the height gives a capsule: the short sides become full
        // semicircles rather than merely rounded corners.
        container.layer?.cornerRadius = Self.size.height / 2
        container.layer?.masksToBounds = true
        container.autoresizingMask = [.width, .height]

        // Tight insets: at this size the capsule's rounded ends already
        // provide the visual margin, so padding it further would leave
        // the wave a sliver.
        waveform.frame = container.bounds.insetBy(dx: 12, dy: 6)
        waveform.autoresizingMask = [.width, .height]
        container.addSubview(waveform)

        contentView = container
    }

    /// Never key, never main — see the class note.
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    // MARK: - Lifecycle

    func show() {
        reposition()
        waveform.reset()
        waveform.startAnimating()
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
            self?.waveform.stopAnimating()
        }
    }

    func update(peak: Int) {
        waveform.append(peak: peak)
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
