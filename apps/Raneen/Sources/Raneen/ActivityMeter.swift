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
/// boundary rather than dance.
final class ActivityMeter: NSView {

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

    /// Interval between updates from the helper.
    private static let updateInterval: TimeInterval = 0.02

    /// Slightly longer, so a late update interrupts an animation still in
    /// flight rather than leaving a bar frozen. An interrupted implicit
    /// animation resumes from the current presentation value, so the
    /// motion stays continuous either way.
    private static let animationDuration: CFTimeInterval = 0.024

    /// Below this it is a quiet room. RMS, not peak — silence measures
    /// tens, ordinary speech measures thousands.
    static let noiseFloor: CGFloat = 60

    /// The scale never drops below this, so room hiss is not amplified
    /// into a dancing meter the moment you stop talking.
    static let minimumCeiling: CGFloat = 900

    /// Per 20 ms update: roughly a two-second half-life.
    static let ceilingDecay: CGFloat = 0.993

    /// Brand orange on the panel's black, deliberately the same colour
    /// the menu-bar mark turns while recording.
    ///
    /// Reusing `StatusIcon.brandColor` rather than restating the hex is
    /// the point: the two indicators are saying the same thing in two
    /// places, and they should not be able to drift into two oranges.
    static let barColor = StatusIcon.brandColor

    private var heights = [CGFloat](repeating: 0, count: barCount)
    private var bars: [CALayer] = []

    private var ceiling = ActivityMeter.minimumCeiling

    /// Updates waiting to be shown, one released per `updateInterval`.
    private var pending: [Int] = []
    private var draining = false

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.masksToBounds = false
        buildBars()
    }

    required init?(coder: NSCoder) {
        fatalError("not used")
    }

    // MARK: - Input

    /// Feed per-block loudness, oldest first. Main thread only.
    func append(blocks: [Int]) {
        guard !blocks.isEmpty else { return }
        pending.append(contentsOf: blocks)
        // A backlog would show audio late, and a meter that is behind is
        // worse than one that skipped: it says you are speaking when you
        // have stopped.
        if pending.count > 8 {
            pending.removeFirst(pending.count - 8)
        }
        startDraining()
    }

    /// 0...1 for one loudness reading, against the current ceiling.
    ///
    /// Square root rather than linear because hearing is compressive:
    /// linear scaling makes ordinary speech look timid and wastes the top
    /// of the range on shouting.
    static func normalise(level: Int, ceiling: CGFloat, noiseFloor: CGFloat = noiseFloor)
        -> CGFloat
    {
        let above = CGFloat(max(0, level)) - noiseFloor
        guard above > 0 else { return 0 }
        return min(1, sqrt(above / max(ceiling - noiseFloor, 1)))
    }

    /// Where the ceiling goes next: up instantly, down slowly.
    ///
    /// Rising immediately matters — a sound louder than anything before it
    /// must not clip while the scale catches up. Falling slowly matters
    /// for the opposite reason: a scale that dropped as fast as it rose
    /// would pump between every word.
    static func nextCeiling(
        current: CGFloat,
        level: CGFloat,
        decay: CGFloat = ceilingDecay,
        minimum: CGFloat = minimumCeiling
    ) -> CGFloat {
        max(minimum, max(level, current * decay))
    }

    /// Peak-meter ballistics: rise fast, ease down.
    ///
    /// `attack` of 1 means jump straight to the target. Below that the bar
    /// closes a fraction of the gap per update, which is what lets the
    /// outer bars trail the centre.
    static func nextHeight(
        current: CGFloat,
        target: CGFloat,
        attack: CGFloat = 1,
        release: CGFloat = centreRelease
    ) -> CGFloat {
        if target > current { return current + (target - current) * attack }
        return max(target, current * release)
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
        pending.removeAll()
        heights = [CGFloat](repeating: 0, count: Self.barCount)
        ceiling = Self.minimumCeiling
        apply(animated: false)
    }

    /// Kept for the panel's lifecycle. There is no timer to start — Core
    /// Animation runs the motion — so this only puts the bars in a known
    /// state before the panel fades in.
    func startAnimating() {
        apply(animated: false)
    }

    func stopAnimating() {
        pending.removeAll()
    }

    // MARK: - Draining

    private func startDraining() {
        guard !draining else { return }
        draining = true
        step()
    }

    /// Show one update, then schedule the next.
    ///
    /// Four arrive together every 80 ms. Applying all four at once would
    /// throw away three quarters of the resolution that measuring them
    /// separately bought; spacing them out is what makes the meter track
    /// syllables rather than words.
    private func step() {
        guard !pending.isEmpty else {
            draining = false
            return
        }
        let level = pending.removeFirst()
        ceiling = Self.nextCeiling(current: ceiling, level: CGFloat(max(0, level)))
        let loudness = Self.normalise(level: level, ceiling: ceiling)

        for index in heights.indices {
            let target = loudness * Self.envelope(bar: index, of: heights.count)
            heights[index] = Self.nextHeight(
                current: heights[index],
                target: target,
                attack: Self.responsiveness(bar: index, of: heights.count),
                release: Self.releaseRate(bar: index, of: heights.count)
            )
        }
        apply(animated: true)

        DispatchQueue.main.asyncAfter(deadline: .now() + Self.updateInterval) { [weak self] in
            self?.step()
        }
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
