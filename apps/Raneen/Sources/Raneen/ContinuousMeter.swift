import AppKit

/// Shared machinery for the indicator styles that keep moving on their own.
///
/// **The bar row only ever moves when audio arrives; these do not.** A ring
/// that turns and embers that drift say "still listening" during the pause
/// between two sentences, which is exactly when the user looks at the panel
/// to check it has not given up. That needs a clock of its own, so this
/// owns one — and owns stopping it, because a timer left running behind a
/// hidden panel is a redraw a second forever.
///
/// Subclasses draw in `render(into:size:)` and read `loudness` (0...1) and
/// `elapsed` (seconds since the panel appeared). They are given nothing
/// else on purpose: an indicator is a picture of *now*, not a chart of the
/// recent past, and a subclass that kept history would be building one.
class ContinuousMeter: NSView, IndicatorView {

    /// 60 Hz. The level itself only updates at 50 Hz, but rotation is
    /// continuous and reads as stuttering below this.
    static let frameInterval: TimeInterval = 1.0 / 60

    /// Brand orange on the panel's black, the same colour the menu-bar mark
    /// turns while recording. Reusing `StatusIcon.brandColor` rather than
    /// restating the hex is the point: every indicator is saying the same
    /// thing, and they must not be able to drift into three oranges.
    static let emberColor = StatusIcon.brandColor

    /// How much loudness a shape keeps per update while falling.
    static let release: CGFloat = 0.90

    /// 0...1, eased. Read this, not the raw level.
    private(set) var loudness: CGFloat = 0

    /// Seconds since `startAnimating`. Motion is a function of this rather
    /// than of a tick count, so a dropped frame skips ahead instead of
    /// slowing the whole animation down.
    private(set) var elapsed: CFTimeInterval = 0

    private var stream: LevelStream?
    private var timer: Timer?
    private var lastTick: CFTimeInterval = 0

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        stream = LevelStream { [weak self] value in
            guard let self else { return }
            self.loudness = LevelStream.ease(
                current: self.loudness, target: value, release: Self.release)
        }
    }

    required init?(coder: NSCoder) {
        fatalError("not used")
    }

    // MARK: - IndicatorView

    func append(blocks: [Int]) {
        stream?.append(blocks: blocks)
    }

    func reset() {
        stream?.reset()
        loudness = 0
        elapsed = 0
        needsDisplay = true
    }

    func startAnimating() {
        guard timer == nil else { return }
        lastTick = CACurrentMediaTime()
        let timer = Timer(timeInterval: Self.frameInterval, repeats: true) { [weak self] _ in
            self?.tick()
        }
        // `.common`, so the animation does not freeze while a menu is open.
        // The status menu is one keystroke away from the thing this panel
        // is reporting on, and stopping there would look like a hang.
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    func stopAnimating() {
        timer?.invalidate()
        timer = nil
        stream?.stop()
    }

    deinit {
        timer?.invalidate()
    }

    // MARK: - Frame

    private func tick() {
        let now = CACurrentMediaTime()
        // Clamped: waking from sleep hands back a delta of minutes, and a
        // shape advanced by that lands somewhere arbitrary.
        let delta = min(0.1, max(0, now - lastTick))
        elapsed += delta
        lastTick = now
        advance(by: delta)
        needsDisplay = true
    }

    /// Subclass hook, called once per frame with the elapsed seconds.
    ///
    /// Where anything that moves on its own belongs — a rotation, an orbit.
    /// Doing it in `render` instead would tie the speed to how often the
    /// view happens to be drawn.
    func advance(by delta: CFTimeInterval) {}

    // MARK: - Drawing

    /// Drawn behind every mark, and it is what makes a backdrop optional.
    ///
    /// Without a black capsule the shape floats on whatever is underneath,
    /// and brand orange on a white document is around 2.3:1 — unreadable.
    /// A dark blur under each mark restores the edge, costs nothing over a
    /// dark background where it is invisible, and does not put a box back
    /// on screen. Centred with no offset: an offset shadow reads as a
    /// drop-shadow effect, which is a style decision, while this is
    /// legibility.
    static let haloRadius: CGFloat = 2.5
    static let haloAlpha: CGFloat = 0.55

    override func draw(_ dirtyRect: NSRect) {
        guard let context = NSGraphicsContext.current?.cgContext,
            bounds.width > 0, bounds.height > 0
        else { return }
        context.setShadow(
            offset: .zero,
            blur: Self.haloRadius,
            color: NSColor.black.withAlphaComponent(Self.haloAlpha).cgColor)
        render(into: context, size: bounds.size)
    }

    /// Subclass hook. The panel behind this is opaque black, so nothing
    /// needs clearing first.
    func render(into context: CGContext, size: CGSize) {}
}
