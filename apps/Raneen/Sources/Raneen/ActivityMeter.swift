import AppKit

/// A row of dots that rise and fall with your voice.
///
/// **This is a level meter, not a waveform.** The distinction matters and
/// an earlier version got it wrong: a waveform scrolls, so most of what
/// you see is a record of the recent past, and the eye has to track
/// something moving to read it. What this needs to answer is simpler and
/// entirely about *now* — can it hear me, and is it hearing me at this
/// instant. Fixed positions answer that at a glance; a timeline makes you
/// read a chart.
///
/// ## Loudness, shaped — not a picture of the audio
///
/// This went through two wrong versions worth recording, because both
/// were *more* faithful to the audio and both looked worse.
///
/// A scrolling waveform showed the recent past, so reading it meant
/// tracking something moving. Per-frequency bands showed the spectrum, so
/// the bars moved independently and it read as a chart of the signal.
/// Both answered "what does the audio look like". The panel is only ever
/// asked "is it hearing me, and how strongly" — and a picture of the
/// audio is a worse answer to that than a shape that just gets bigger.
///
/// So: **one loudness value, drawn as a symmetric shape.** Bars are a
/// fixed envelope — tallest at the centre, tapering to the ends — scaled
/// bodily by how loud you are. Mirrored pairs are equal by construction,
/// not by coincidence, so the two halves cannot drift apart.
///
/// The one thing that keeps it from looking like a rectangle being
/// stretched: **every bar moves at its own speed.** The centre snaps up
/// and drops away quickly; the ends drift up behind it and linger. So a
/// sound swells outward from the middle and drains back inward, and the
/// row changes shape between one syllable and the next rather than only
/// changing size. Symmetry survives all of it because every varying
/// quantity is a function of distance from the centre, and a bar and its
/// mirror are the same distance away.
///
/// ## Ballistics
///
/// Instant attack, gradual release — the same rule real peak meters use.
/// A bar jumps to a loud sound the moment it arrives, because lagging
/// there feels unresponsive, and then falls back smoothly, because a bar
/// that dropped as fast as it rose would flicker on every syllable
/// boundary rather than dance. The rule itself lives in `LevelStream`,
/// which every indicator style shares; what is here is the part that is
/// specific to a row of bars.
///
/// This is the default style and the calmest one. Unlike `BloomMeter` and
/// `SwarmMeter` it has no clock of its own — it moves only when audio
/// arrives, which is why it is still.
final class ActivityMeter: NSView, IndicatorView {

    /// Odd, so there is a true centre bar for the shape to peak on.
    private static let barCount = 9
    private static let spacing: CGFloat = 2.8

    /// How tall the outermost bar gets relative to the centre.
    ///
    /// Low on purpose: the ends stay dots and only the inner bars really
    /// move. That is what gives the row somewhere to grow *into* — when
    /// every bar rises the same proportion, the whole thing scales like a
    /// stretched picture rather than changing shape.
    private static let edgeHeight: CGFloat = 0.12

    /// How much of the gap to its target the outermost bar closes per
    /// update, against 1.0 — jump straight there — at the centre.
    private static let edgeAttack: CGFloat = 0.25

    /// How much height a bar keeps per update while falling, at the
    /// centre and at the ends.
    ///
    /// **They differ, and that is the point.** With one rate every bar
    /// rose and fell together and the row moved as a single object. The
    /// centre now snaps up and drops away quickly while the ends drift up
    /// behind it and linger — so the shape genuinely changes between one
    /// syllable and the next instead of only changing size.
    private static let centreRelease: CGFloat = 0.90
    private static let edgeRelease: CGFloat = 0.965

    /// Slightly longer than `LevelStream.updateInterval`, so a late update
    /// interrupts an animation still in flight rather than leaving a bar
    /// frozen. An interrupted implicit animation resumes from the current
    /// presentation value, so the motion stays continuous either way.
    private static let animationDuration: CFTimeInterval = 0.024

    /// Brand orange on the panel's black, deliberately the same colour
    /// the menu-bar mark turns while recording.
    ///
    /// Reusing `StatusIcon.brandColor` rather than restating the hex is
    /// the point: the two indicators are saying the same thing in two
    /// places, and they should not be able to drift into two oranges.
    static let barColor = StatusIcon.brandColor

    private var heights = [CGFloat](repeating: 0, count: barCount)
    private var bars: [CALayer] = []

    private var stream: LevelStream?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.masksToBounds = false
        buildBars()
        stream = LevelStream { [weak self] loudness in self?.advance(to: loudness) }
    }

    required init?(coder: NSCoder) {
        fatalError("not used")
    }

    // MARK: - Input

    /// Feed per-block loudness, oldest first. Main thread only.
    func append(blocks: [Int]) {
        stream?.append(blocks: blocks)
    }

    /// The fixed silhouette: tallest in the middle, tapering to the ends.
    ///
    /// Mirrored by construction — the weight depends only on how far a bar
    /// is from the centre, so bar *i* and bar *n-1-i* are the same number
    /// rather than two numbers that happen to agree. Symmetry that is
    /// computed cannot drift; symmetry that is arranged can.
    static func envelope(bar: Int, of count: Int) -> CGFloat {
        guard count > 1 else { return 1 }
        return edgeHeight + (1 - edgeHeight) * centreness(bar: bar, of: count)
    }

    /// How close to the centre a bar is: 1 in the middle, 0 at the ends.
    /// Everything that varies across the row is a function of this, which
    /// is what keeps all of it mirrored.
    private static func centreness(bar: Int, of count: Int) -> CGFloat {
        guard count > 1 else { return 1 }
        return sin(CGFloat(bar) / CGFloat(count - 1) * .pi)
    }

    /// How quickly a bar rises: instantly at the centre, gradually at the
    /// ends, so a sound swells outward from the middle.
    static func responsiveness(bar: Int, of count: Int) -> CGFloat {
        guard count > 1 else { return 1 }
        return edgeAttack + (1 - edgeAttack) * centreness(bar: bar, of: count)
    }

    /// How slowly a bar falls: quickly at the centre, lingering at the
    /// ends. Together with `responsiveness` this is what makes the row
    /// change shape rather than only change size.
    static func releaseRate(bar: Int, of count: Int) -> CGFloat {
        guard count > 1 else { return centreRelease }
        return edgeRelease - (edgeRelease - centreRelease) * centreness(bar: bar, of: count)
    }

    func reset() {
        stream?.reset()
        heights = [CGFloat](repeating: 0, count: Self.barCount)
        apply(animated: false)
    }

    /// Kept for the panel's lifecycle. There is no timer to start — Core
    /// Animation runs the motion — so this only puts the bars in a known
    /// state before the panel fades in.
    func startAnimating() {
        apply(animated: false)
    }

    func stopAnimating() {
        stream?.stop()
    }

    // MARK: - Motion

    /// One loudness reading, applied to every bar.
    private func advance(to loudness: CGFloat) {
        for index in heights.indices {
            let target = loudness * Self.envelope(bar: index, of: heights.count)
            heights[index] = LevelStream.ease(
                current: heights[index],
                target: target,
                attack: Self.responsiveness(bar: index, of: heights.count),
                release: Self.releaseRate(bar: index, of: heights.count)
            )
        }
        apply(animated: true)
    }

    // MARK: - Layers

    private func buildBars() {
        bars.forEach { $0.removeFromSuperlayer() }
        bars = (0..<Self.barCount).map { _ in
            let bar = CALayer()
            bar.backgroundColor = Self.barColor.cgColor
            // Grows symmetrically about the centre line, so one animated
            // height gives the mirrored shape without a second layer.
            bar.anchorPoint = CGPoint(x: 0.5, y: 0.5)
            layer?.addSublayer(bar)
            return bar
        }
    }

    override func layout() {
        super.layout()
        apply(animated: false)
    }

    private func apply(animated: Bool) {
        guard !bars.isEmpty, bounds.width > 0, bounds.height > 0 else { return }

        let totalSpacing = Self.spacing * CGFloat(max(1, bars.count - 1))
        let barWidth = max(1.0, (bounds.width - totalSpacing) / CGFloat(bars.count))
        let midY = bounds.midY
        let maxHalf = bounds.height / 2
        // At silence the bars are a row of dots the same size as their
        // width — present, but plainly not hearing anything.
        let minHalf = barWidth / 2

        CATransaction.begin()
        if animated {
            CATransaction.setAnimationDuration(Self.animationDuration)
            // Linear, not ease-in-out. These run back to back, and easing
            // each would put a deceleration at every update boundary.
            CATransaction.setAnimationTimingFunction(CAMediaTimingFunction(name: .linear))
        } else {
            CATransaction.setDisableActions(true)
        }

        for (index, value) in heights.enumerated() {
            let half = minHalf + value * (maxHalf - minHalf)
            let bar = bars[index]
            bar.bounds = CGRect(x: 0, y: 0, width: barWidth, height: half * 2)
            bar.position = CGPoint(
                x: CGFloat(index) * (barWidth + Self.spacing) + barWidth / 2,
                y: midY
            )
            bar.cornerRadius = barWidth / 2
        }
        CATransaction.commit()
    }
}
