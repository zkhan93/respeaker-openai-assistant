import AppKit

/// Spokes around a ring, reaching outward with your voice.
///
/// ## One loudness value, drawn as a shape
///
/// The same rule the bar row is built on, and for the same reason: what
/// the panel is asked is "can it hear me, and how strongly", and a picture
/// of the audio is a worse answer than a shape that just gets bigger. So
/// there is **no spectrum here** — every spoke is driven by the single
/// loudness reading, and what varies around the ring is a fixed weighting,
/// not a frequency band. A per-band version was tried in the bar row and
/// read as a chart of the signal rather than as an indicator.
///
/// ## What stops it looking like a circle being scaled
///
/// Two things, both of which have to be there:
///
/// * **The weighting rotates.** Three lobes turning slowly, so the outline
///   changes shape between one syllable and the next rather than only
///   changing size. It turns while you are silent too — that is the whole
///   argument for this style over the bar row, which is motionless in a
///   pause and therefore says nothing during one.
/// * **Spokes fall at their own speeds.** A fixed rate per position, so
///   after a loud moment some are still up while their neighbours have
///   gone. With one rate the ring drained as a single object.
///
/// Rotation speeds up as you speak, so the reaction to a loud syllable is
/// a shape that lunges rather than one that only inflates.
final class BloomMeter: ContinuousMeter {

    /// Enough that the outline reads as a curve rather than a polygon, few
    /// enough that the gaps between spokes stay visible at 74 points. At 60
    /// they merged into a solid fan and the style lost the thing that makes
    /// it legible — you could see the outline move but not what it was made
    /// of.
    static let spokeCount = 40

    /// Fractions of the radius: where the spokes start, and how far the
    /// loudest one reaches. The ring is well inside the panel edge because
    /// a shape that touches the border looks clipped rather than contained.
    static let ringRadius: CGFloat = 0.36
    static let maximumReach: CGFloat = 0.58

    /// A spoke never disappears. At silence they are a ring of ticks —
    /// present, but plainly not hearing anything — for the same reason the
    /// bar row settles to a row of dots rather than to nothing.
    static let restingLength: CGFloat = 1.5

    /// Radians per second, at silence and at full voice. The idle rate is
    /// slow on purpose: this floats over the user's work, and something
    /// turning quickly in the corner of the eye is a distraction, not an
    /// indicator.
    static let idleSpin: CGFloat = 0.55
    static let voiceSpin: CGFloat = 1.9

    private var heights = [CGFloat](repeating: 0, count: BloomMeter.spokeCount)
    private var phase: CGFloat = 0

    // MARK: - Shape

    /// How far out this spoke reaches, relative to the loudest one.
    ///
    /// Two lobed terms at different rates, turning in opposite directions:
    /// one alone gives a shape with an obvious period that the eye locks
    /// onto and reads as a spinning object, and the two together drift in
    /// and out of alignment so the outline never quite repeats.
    ///
    /// Never reaches zero — a spoke pinned flat while its neighbours move
    /// reads as a rendering fault.
    static func weight(spoke: Int, of count: Int, phase: CGFloat) -> CGFloat {
        guard count > 0 else { return 1 }
        let angle = CGFloat(spoke) / CGFloat(count) * 2 * .pi
        let lobes = 0.5 + 0.5 * sin(3 * angle + phase)
        let slow = 0.5 + 0.5 * sin(2 * angle - phase * 0.6)
        // The floor is low on purpose. At 0.45 the shortest spoke was still
        // nearly half the longest, so the ring read as a full disc that
        // wobbled rather than as a shape with a direction to it.
        return 0.22 + 0.55 * lobes + 0.23 * slow
    }

    /// How slowly this spoke falls. A property of the position, not of the
    /// rotating shape, so the texture stays put while the outline turns.
    static func releaseRate(spoke: Int, of count: Int) -> CGFloat {
        guard count > 1 else { return release }
        // Five interleaved rates rather than a smooth ramp: neighbouring
        // spokes then differ, which is what makes the decay look grainy
        // instead of like a wipe travelling round the ring.
        let step = CGFloat(spoke % 5) / 4
        return 0.86 + 0.10 * step
    }

    /// Radians per second at this loudness.
    static func spin(loudness: CGFloat) -> CGFloat {
        idleSpin + (voiceSpin - idleSpin) * max(0, min(1, loudness))
    }

    // MARK: - Motion

    override func advance(by delta: CFTimeInterval) {
        phase += Self.spin(loudness: loudness) * CGFloat(delta)
        // Wrapped, so the argument to `sin` cannot grow without bound over
        // a long session and start losing precision.
        if phase > 2 * .pi { phase -= 2 * .pi }

        for index in heights.indices {
            let target = loudness * Self.weight(spoke: index, of: heights.count, phase: phase)
            heights[index] = LevelStream.ease(
                current: heights[index],
                target: target,
                release: Self.releaseRate(spoke: index, of: heights.count)
            )
        }
    }

    // MARK: - Drawing

    override func render(into context: CGContext, size: CGSize) {
        let centre = CGPoint(x: size.width / 2, y: size.height / 2)
        let radius = min(size.width, size.height) / 2 - 1
        let ring = radius * Self.ringRadius
        let reach = radius * Self.maximumReach

        context.setStrokeColor(Self.emberColor.withAlphaComponent(0.30).cgColor)
        context.setLineWidth(1)
        context.strokeEllipse(
            in: CGRect(
                x: centre.x - ring, y: centre.y - ring, width: ring * 2, height: ring * 2))

        context.setLineWidth(1.5)
        context.setLineCap(.round)
        for (index, height) in heights.enumerated() {
            let angle = CGFloat(index) / CGFloat(heights.count) * 2 * .pi
            let length = Self.restingLength + height * reach
            let unit = CGPoint(x: cos(angle), y: sin(angle))
            let start = CGPoint(
                x: centre.x + unit.x * (ring + 2), y: centre.y + unit.y * (ring + 2))
            let end = CGPoint(
                x: start.x + unit.x * length, y: start.y + unit.y * length)

            // Brightness tracks length as well as position tracking it.
            // Length alone is legible at 74 points but only just, and the
            // two together are what make a quiet room look quiet.
            //
            // The floor is 0.55 rather than 0.40 because there is no black
            // capsule behind this any more: a resting tick at 0.40 over a
            // white document was there, but only if you already knew to
            // look for it.
            context.setStrokeColor(
                Self.emberColor.withAlphaComponent(0.55 + 0.45 * height).cgColor)
            context.beginPath()
            context.move(to: start)
            context.addLine(to: end)
            context.strokePath()
        }
    }
}
