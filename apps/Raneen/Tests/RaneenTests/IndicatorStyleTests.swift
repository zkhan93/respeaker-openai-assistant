import XCTest

@testable import Raneen

/// The choice of animation, and the geometry each one is built on.
///
/// The motion itself is visual and only a human can judge it. What is
/// checkable is the part that goes wrong silently: a stored style that
/// does not survive a relaunch, a shape that collapses at silence, or a
/// swarm whose embers all end up in the same quadrant.
final class IndicatorStyleTests: XCTestCase {

    private var defaults: UserDefaults!

    override func setUp() {
        super.setUp()
        defaults = UserDefaults(suiteName: "raneen.tests.indicator")
        defaults.removePersistentDomain(forName: "raneen.tests.indicator")
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: "raneen.tests.indicator")
        defaults = nil
        super.tearDown()
    }

    // MARK: - The stored choice

    /// A new install gets the calmest style, not whichever case happens to
    /// be first in the enum.
    func testTheDefaultIsTheBarRow() {
        XCTAssertEqual(IndicatorPreference.current(defaults), .bars)
        XCTAssertEqual(IndicatorStyle.fallback, .bars)
    }

    func testAChoiceSurvivesARelaunch() {
        for style in IndicatorStyle.allCases {
            IndicatorPreference.save(style, to: defaults)
            XCTAssertEqual(IndicatorPreference.current(defaults), style)
        }
    }

    /// A style written by a newer version has to degrade to the default
    /// rather than crash or draw nothing.
    func testAnUnknownStyleFallsBackRatherThanFailing() {
        defaults.set("kaleidoscope", forKey: IndicatorPreference.key)
        XCTAssertEqual(IndicatorPreference.current(defaults), .bars)
    }

    /// Reading must not write. The same mistake in `SettingsStore` turned a
    /// momentary bad read into a permanently broken configuration on disk.
    func testReadingDoesNotPersistAnything() {
        _ = IndicatorPreference.current(defaults)
        XCTAssertNil(defaults.string(forKey: IndicatorPreference.key))
    }

    // MARK: - Panel geometry

    /// Every style has to fit the panel's job: over the user's work, read
    /// at a glance, then forgotten.
    func testEveryStyleIsSmallEnoughToFloatOverSomeoneElsesWindow() {
        for style in IndicatorStyle.allCases {
            XCTAssertLessThanOrEqual(style.panelSize.width, 100, "\(style.label) is too wide")
            XCTAssertLessThanOrEqual(style.panelSize.height, 100, "\(style.label) is too tall")
        }
    }

    /// The inset cannot eat the drawing. A negative content rect draws
    /// nothing at all, and the panel would show as an empty capsule.
    func testTheInsetLeavesSomethingToDraw() {
        for style in IndicatorStyle.allCases {
            let content = NSRect(origin: .zero, size: style.panelSize)
                .insetBy(dx: style.contentInset.width, dy: style.contentInset.height)
            XCTAssertGreaterThan(content.width, 20, "\(style.label) has no room across")
            XCTAssertGreaterThan(content.height, 10, "\(style.label) has no room down")
        }
    }

    /// Half the height, so the bar row is a capsule and the radial styles
    /// are circles. A larger radius would clip the drawing.
    func testCornersAreNeverLargerThanTheShape() {
        for style in IndicatorStyle.allCases {
            XCTAssertEqual(style.cornerRadius, style.panelSize.height / 2)
            XCTAssertLessThanOrEqual(style.cornerRadius, style.panelSize.width / 2)
        }
    }

    /// The radial styles need square panels or the ring draws as an
    /// ellipse — which is the reason the style owns the panel size at all.
    func testTheRadialStylesAreSquare() {
        for style in [IndicatorStyle.bloom, .swarm] {
            XCTAssertEqual(
                style.panelSize.width, style.panelSize.height, "\(style.label) is not square")
        }
    }

    /// The bar row keeps its capsule and the radial styles float free.
    ///
    /// Not cosmetic: without a backdrop the marks are drawn over an unknown
    /// background, and brand orange on a white document is around 2.3:1.
    /// The halo is what pays for it, so a style that gave up the capsule
    /// without one would be unreadable half the time.
    func testOnlyTheStylesThatCanAffordItDropTheBackdrop() {
        XCTAssertTrue(IndicatorStyle.bars.hasBackdrop, "the bar row cannot carry a halo")
        for style in [IndicatorStyle.bloom, .swarm] {
            XCTAssertFalse(style.hasBackdrop, "\(style.label) should float free")
            XCTAssertTrue(
                style.makeView() is ContinuousMeter,
                "\(style.label) has no backdrop and no halo to replace it")
        }
    }

    /// Dark enough and wide enough to be an edge, small enough not to read
    /// as a drop shadow around a shape that is meant to be floating.
    func testTheHaloIsAnEdgeRatherThanAnEffect() {
        XCTAssertGreaterThan(ContinuousMeter.haloAlpha, 0.4)
        XCTAssertLessThan(ContinuousMeter.haloAlpha, 0.7)
        XCTAssertGreaterThan(ContinuousMeter.haloRadius, 1)
        XCTAssertLessThan(ContinuousMeter.haloRadius, 5)
    }

    func testEveryStyleBuildsItsView() {
        XCTAssertTrue(IndicatorStyle.bars.makeView() is ActivityMeter)
        XCTAssertTrue(IndicatorStyle.bloom.makeView() is BloomMeter)
        XCTAssertTrue(IndicatorStyle.swarm.makeView() is SwarmMeter)
    }

    /// One colour for "recording", in all three styles and in the menu bar.
    /// Restating the hex per style is how three oranges happen.
    func testEveryStyleUsesTheSameBrandColour() {
        XCTAssertEqual(ContinuousMeter.emberColor, StatusIcon.brandColor)
        XCTAssertEqual(ActivityMeter.barColor, StatusIcon.brandColor)
    }
}

// MARK: - Bloom (a shape that turns)

extension IndicatorStyleTests {

    private var spokes: Int { BloomMeter.spokeCount }

    /// No spoke ever collapses. One pinned flat while its neighbours move
    /// reads as a rendering fault rather than as quiet.
    func testEverySpokeKeepsSomeLength() {
        for phase in stride(from: CGFloat(0), to: 6.3, by: 0.35) {
            for spoke in 0..<spokes {
                let weight = BloomMeter.weight(spoke: spoke, of: spokes, phase: phase)
                XCTAssertGreaterThan(weight, 0.2, "spoke \(spoke) collapsed at phase \(phase)")
                XCTAssertLessThanOrEqual(weight, 1.0001, "spoke \(spoke) overran the panel")
            }
        }
    }

    /// The outline has to be lopsided at any instant. A weighting that came
    /// out uniform would leave a circle that only changes size.
    func testTheOutlineIsNeverACircle() {
        for phase in stride(from: CGFloat(0), to: 6.3, by: 0.7) {
            let weights = (0..<spokes).map {
                BloomMeter.weight(spoke: $0, of: spokes, phase: phase)
            }
            let spread = (weights.max() ?? 0) - (weights.min() ?? 0)
            XCTAssertGreaterThan(spread, 0.35, "the ring is round at phase \(phase)")
        }
    }

    /// …and it has to be a *different* lopsided shape as it turns, or the
    /// eye reads one rigid object rotating.
    func testTheShapeChangesAsItTurns() {
        let before = (0..<spokes).map { BloomMeter.weight(spoke: $0, of: spokes, phase: 0) }
        let after = (0..<spokes).map { BloomMeter.weight(spoke: $0, of: spokes, phase: 1.2) }
        let moved = zip(before, after).filter { abs($0 - $1) > 0.05 }.count
        XCTAssertGreaterThan(moved, spokes / 2, "the whole outline moved as one piece")
    }

    /// It turns while you are silent. That is the entire argument for this
    /// style over the bar row, which is motionless in a pause.
    func testItKeepsTurningInSilence() {
        XCTAssertGreaterThan(BloomMeter.spin(loudness: 0), 0)
        XCTAssertGreaterThan(BloomMeter.spin(loudness: 1), BloomMeter.spin(loudness: 0))
        // Clamped: a level above the ceiling must not spin it faster still.
        XCTAssertEqual(BloomMeter.spin(loudness: 4), BloomMeter.spin(loudness: 1))
    }

    /// Neighbouring spokes fall at different rates, so the decay looks
    /// grainy rather than like a wipe travelling round the ring.
    func testNeighbouringSpokesDoNotFallTogether() {
        var differences = 0
        for spoke in 0..<(spokes - 1) {
            let a = BloomMeter.releaseRate(spoke: spoke, of: spokes)
            let b = BloomMeter.releaseRate(spoke: spoke + 1, of: spokes)
            if abs(a - b) > 0.001 { differences += 1 }
        }
        XCTAssertEqual(differences, spokes - 1, "some neighbours share a release rate")
    }

    /// Every spoke still settles. A rate at or above 1 leaves one twitching
    /// in a quiet room forever.
    func testEverySpokeSettlesToSilence() {
        for spoke in 0..<spokes {
            let release = BloomMeter.releaseRate(spoke: spoke, of: spokes)
            XCTAssertGreaterThan(release, 0.8, "spoke \(spoke) drops instead of easing")
            XCTAssertLessThan(release, 1.0, "spoke \(spoke) never comes down")

            var height: CGFloat = 1
            for _ in 0..<200 {
                height = LevelStream.ease(current: height, target: 0, release: release)
            }
            XCTAssertLessThan(height, 0.02, "spoke \(spoke) is still moving after silence")
        }
    }
}

// MARK: - Swarm (embers in orbit)

extension IndicatorStyleTests {

    private var embers: Int { SwarmMeter.particleCount }

    /// Inside the panel, and clear of the centre ember — an orbit of zero
    /// puts a dot on top of the core and reads as a smudge.
    func testEveryEmberOrbitsWithinThePanel() {
        for index in 0..<embers {
            let orbit = SwarmMeter.orbit(of: index)
            XCTAssertGreaterThanOrEqual(orbit, SwarmMeter.innerOrbit)
            XCTAssertLessThanOrEqual(orbit, SwarmMeter.outerOrbit)
            XCTAssertLessThanOrEqual(orbit, 1.0, "ember \(index) orbits outside the panel")
        }
    }

    /// The layout is deterministic, so a bad-looking arrangement is a bug
    /// that can be reproduced rather than one seen once at launch.
    func testTheLayoutIsTheSameOnEveryLaunch() {
        let first = (0..<embers).map { SwarmMeter.startingAngle(of: $0) }
        let second = (0..<embers).map { SwarmMeter.startingAngle(of: $0) }
        XCTAssertEqual(first, second)
    }

    /// Spread all the way round. A clump on one side is what a random
    /// layout produces occasionally and an irrational stride never does.
    func testTheEmbersAreSpreadRightRound() {
        var quadrants = [0, 0, 0, 0]
        for index in 0..<embers {
            let angle = SwarmMeter.startingAngle(of: index)
            XCTAssertGreaterThanOrEqual(angle, 0)
            XCTAssertLessThan(angle, 2 * .pi + 0.0001)
            quadrants[min(3, Int(angle / (.pi / 2)))] += 1
        }
        for (index, count) in quadrants.enumerated() {
            XCTAssertGreaterThan(count, embers / 8, "quadrant \(index) is nearly empty")
        }
    }

    /// Radius and angle must not advance together, or the swarm falls into
    /// a single spiral arm instead of filling the disc.
    func testOrbitAndAngleDoNotShareAStride() {
        let angles = (0..<embers).map { SwarmMeter.startingAngle(of: $0) / (2 * .pi) }
        let orbits = (0..<embers).map { SwarmMeter.orbit(of: $0) }
        let meanAngle = angles.reduce(0, +) / CGFloat(embers)
        let meanOrbit = orbits.reduce(0, +) / CGFloat(embers)
        var covariance: CGFloat = 0
        for index in 0..<embers {
            covariance += (angles[index] - meanAngle) * (orbits[index] - meanOrbit)
        }
        XCTAssertLessThan(
            abs(covariance / CGFloat(embers)), 0.02, "the embers line up along one arm")
    }

    /// Inner embers travel faster — the thing that stops the swarm turning
    /// as one rigid disc.
    func testInnerEmbersOutrunOuterOnes() {
        let inner = SwarmMeter.angularSpeed(orbit: SwarmMeter.innerOrbit, loudness: 0)
        let outer = SwarmMeter.angularSpeed(orbit: SwarmMeter.outerOrbit, loudness: 0)
        XCTAssertGreaterThan(inner, outer)
        // …but not so much faster that the middle blurs.
        XCTAssertLessThan(inner, outer * 4)
    }

    /// It keeps moving in silence, and speeds up as you speak.
    func testTheOrbitNeverStops() {
        let quiet = SwarmMeter.angularSpeed(orbit: 0.6, loudness: 0)
        let loud = SwarmMeter.angularSpeed(orbit: 0.6, loudness: 1)
        XCTAssertGreaterThan(quiet, 0)
        XCTAssertGreaterThan(loud, quiet)
        XCTAssertEqual(loud, SwarmMeter.angularSpeed(orbit: 0.6, loudness: 9))
    }

    /// Your voice pulls the swarm in — but never all the way. Collapsing to
    /// the centre would stop it being a swarm at the moment it is meant to
    /// show that it is hearing you.
    func testSpeakingTightensTheSwarmWithoutCollapsingIt() {
        let resting = SwarmMeter.radius(orbit: 0.9, loudness: 0)
        let tightened = SwarmMeter.radius(orbit: 0.9, loudness: 1)
        XCTAssertEqual(resting, 0.9, accuracy: 0.0001)
        XCTAssertLessThan(tightened, resting)
        XCTAssertGreaterThan(tightened, resting * 0.5, "the swarm collapsed onto the centre")
    }

    /// A level above the ceiling must not pull the embers through the
    /// middle and out the other side.
    func testAnOverloadedLevelDoesNotInvertTheOrbit() {
        XCTAssertEqual(
            SwarmMeter.radius(orbit: 0.9, loudness: 12),
            SwarmMeter.radius(orbit: 0.9, loudness: 1))
        XCTAssertGreaterThan(SwarmMeter.radius(orbit: 0.9, loudness: 12), 0)
        XCTAssertEqual(
            SwarmMeter.radius(orbit: 0.9, loudness: -3), 0.9, accuracy: 0.0001)
    }
}

// MARK: - The preview in the settings window

extension IndicatorStyleTests {

    /// The preview has to look like speech: pauses, and peaks that fill the
    /// shape. A signal that never reached the ceiling would preview an
    /// animation more timid than the real one.
    func testTheSyntheticVoiceHasPeaksAndPauses() {
        var peak = 0
        var silences = 0
        var time: TimeInterval = 0
        while time < 20 {
            let level = IndicatorPreviewView.level(at: time)
            XCTAssertGreaterThanOrEqual(level, 0, "a negative level reads as silence anyway")
            peak = max(peak, level)
            if CGFloat(level) < LevelStream.noiseFloor { silences += 1 }
            time += LevelStream.updateInterval
        }
        XCTAssertGreaterThan(
            CGFloat(peak), LevelStream.minimumCeiling,
            "the preview never fills the shape")
        XCTAssertGreaterThan(silences, 20, "the preview never pauses for breath")
    }
}
