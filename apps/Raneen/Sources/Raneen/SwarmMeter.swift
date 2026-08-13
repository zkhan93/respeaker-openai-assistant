import AppKit

/// Embers orbiting a centre, drawn inward by your voice.
///
/// Same rule as the other two styles: **one loudness value**, no spectrum.
/// Here it moves the swarm rather than stretching a shape — louder speech
/// pulls every ember toward the centre and speeds the whole orbit up, so a
/// sentence reads as the swarm tightening and a pause as it relaxing.
///
/// ## Why it looks alive when nothing is being said
///
/// The orbit never stops, and the embers do not share an angular speed:
/// the inner ones travel fastest, the way anything orbiting does. That
/// alone is enough for the swarm to keep rearranging itself while the room
/// is quiet — which is the point of this style. The bar row is motionless
/// in a pause and therefore says nothing during one, and a pause is
/// exactly when someone glances at the panel to check it is still there.
///
/// ## Why the layout is computed rather than random
///
/// Positions come from an irrational stride, so they are spread evenly and
/// **identical on every launch**. A random layout is a layout that is
/// occasionally bad — a clump on one side, a bare quadrant — and a bug you
/// cannot reproduce is a bug you cannot fix.
final class SwarmMeter: ContinuousMeter {

    /// Enough to read as a swarm rather than as countable dots, few enough
    /// that they do not merge into a disc at 74 points.
    static let particleCount = 56

    /// Closest and furthest resting orbit, as fractions of the radius. The
    /// inner bound is not zero: an ember sitting on the centre ember reads
    /// as one smudge, and the hole is what makes the swarm look like an
    /// orbit rather than a cloud.
    static let innerOrbit: CGFloat = 0.30
    static let outerOrbit: CGFloat = 0.96

    /// How much of its orbit an ember gives up at full voice.
    ///
    /// A fifth, not all of it. Collapsing to the centre would be a stronger
    /// signal and a worse one — the swarm would stop being a swarm at
    /// precisely the moment it is meant to show that it is hearing you, and
    /// at a third it already drew the loudest syllables as a blob half the
    /// size of the panel.
    static let inwardPull: CGFloat = 0.20

    /// Radians per second for an ember at the outer orbit, at silence and
    /// at full voice. Inner embers scale up from this.
    static let idleSpeed: CGFloat = 0.42
    static let voiceSpeed: CGFloat = 1.5

    private var angles: [CGFloat] = []

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        angles = (0..<Self.particleCount).map { Self.startingAngle(of: $0) }
    }

    required init?(coder: NSCoder) {
        fatalError("not used")
    }

    // MARK: - Layout

    /// The golden angle. Successive embers land this far apart, which is
    /// the one stride that leaves no gaps and no clusters at any count —
    /// the arrangement a sunflower head uses, for the same reason.
    static let goldenAngle: CGFloat = 2.39996322972865332

    /// Where ember *index* starts.
    static func startingAngle(of index: Int) -> CGFloat {
        let angle = CGFloat(index) * goldenAngle
        return angle.truncatingRemainder(dividingBy: 2 * .pi)
    }

    /// Its resting orbit, as a fraction of the radius.
    ///
    /// **The square root is what makes the disc look evenly covered.** Two
    /// independent strides were tried first — one for the angle, one for
    /// the radius — and they drifted into visible arms with a bare patch
    /// beside them, because spacing radii evenly crowds the inside: a ring
    /// at half the radius has half the circumference to hold the same
    /// number of embers. Taking the root of an even fraction spaces them by
    /// area instead, and pairs with the golden angle to give the spiral
    /// that has no arms in it.
    static func orbit(of index: Int) -> CGFloat {
        let even = (CGFloat(index) + 0.5) / CGFloat(max(1, particleCount))
        return innerOrbit + (outerOrbit - innerOrbit) * sqrt(min(1, even))
    }

    /// Radians per second for an ember at this orbit and this loudness.
    ///
    /// Inner orbits are faster — the real behaviour, and the thing that
    /// keeps the swarm from turning as one rigid disc. Falls off with
    /// radius rather than with the square of it: the physical law is too
    /// steep at this scale and left the outer embers looking frozen.
    static func angularSpeed(orbit: CGFloat, loudness: CGFloat) -> CGFloat {
        let base = idleSpeed + (voiceSpeed - idleSpeed) * max(0, min(1, loudness))
        return base * (outerOrbit / max(orbit, innerOrbit))
    }

    /// Where an ember actually sits: its orbit, pulled in by the voice.
    static func radius(orbit: CGFloat, loudness: CGFloat) -> CGFloat {
        orbit * (1 - inwardPull * max(0, min(1, loudness)))
    }

    // MARK: - Motion

    override func advance(by delta: CFTimeInterval) {
        for index in angles.indices {
            let speed = Self.angularSpeed(orbit: Self.orbit(of: index), loudness: loudness)
            var angle = angles[index] + speed * CGFloat(delta)
            if angle > 2 * .pi { angle -= 2 * .pi }
            angles[index] = angle
        }
    }

    // MARK: - Drawing

    override func render(into context: CGContext, size: CGSize) {
        let centre = CGPoint(x: size.width / 2, y: size.height / 2)
        let radius = min(size.width, size.height) / 2 - 2

        for (index, angle) in angles.enumerated() {
            let orbit = Self.orbit(of: index)
            // A slow bob, so an ember is never exactly where its orbit says
            // it should be. Without it the swarm reads as a set of rings.
            let bob = sin(CGFloat(elapsed) * (0.7 + orbit) + Self.startingAngle(of: index))
            let distance =
                radius * Self.radius(orbit: orbit, loudness: loudness)
                + bob * radius * 0.05 * (0.4 + loudness)
            // Small. At 74 points a dot much over a point across merges
            // with its neighbours and the swarm reads as one blob, which
            // is the failure that costs this style its whole idea.
            let dot = 0.65 + 0.85 * loudness
            // Inner embers are brighter at rest, so the swarm has a centre
            // of gravity to look at when nothing is being said. The floor
            // is lifted for the same reason the bloom's is: with no capsule
            // behind them, embers at 0.30 over a white page vanished.
            let alpha = 0.45 + 0.35 * loudness + 0.20 * (1 - orbit)

            context.setFillColor(Self.emberColor.withAlphaComponent(min(1, alpha)).cgColor)
            context.fillEllipse(
                in: CGRect(
                    x: centre.x + cos(angle) * distance - dot,
                    y: centre.y + sin(angle) * distance - dot,
                    width: dot * 2,
                    height: dot * 2))
        }

        let core = 1.3 + 2.4 * loudness
        context.setFillColor(Self.emberColor.withAlphaComponent(0.85).cgColor)
        context.fillEllipse(
            in: CGRect(
                x: centre.x - core, y: centre.y - core, width: core * 2, height: core * 2))
    }
}
