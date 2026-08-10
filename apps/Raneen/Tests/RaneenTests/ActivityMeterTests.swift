import XCTest

@testable import Raneen

/// The level→height maths. The motion itself is visual and only a human
/// can judge it, but an out-of-range level draws bars outside the panel,
/// and ballistics that are wrong in either direction make the meter read
/// as broken rather than as responsive.
final class ActivityMeterTests: XCTestCase {

    private let ceiling = ActivityMeter.minimumCeiling

    private func value(_ level: Int) -> CGFloat {
        ActivityMeter.normalise(level: level, ceiling: ceiling)
    }

    // MARK: - Range

    func testAQuietRoomIsFlat() {
        // Band magnitudes in a silent room are single digits. If those
        // lifted the bars the meter would claim to hear you when it does
        // not, which is the one thing it must never do.
        XCTAssertEqual(value(0), 0)
        XCTAssertEqual(value(8), 0)
    }

    func testTheNoiseFloorLeavesRoomForQuietSpeech() {
        XCTAssertLessThan(ActivityMeter.noiseFloor, ActivityMeter.minimumCeiling / 4)
    }

    func testTheCeilingIsFullHeight() {
        XCTAssertEqual(value(Int(ceiling)), 1.0, accuracy: 0.0001)
    }

    func testLoudInputIsClamped() {
        XCTAssertEqual(value(32767), 1.0)
    }

    func testNegativeInputDoesNotProduceNegativeBars() {
        XCTAssertEqual(value(-500), 0)
    }

    func testLouderInputAlwaysGivesATallerBar() {
        var previous: CGFloat = -1
        for level in stride(from: 0, through: Int(ceiling), by: 5) {
            let current = value(level)
            XCTAssertGreaterThanOrEqual(current, previous)
            previous = current
        }
    }

    func testTheCurveIsCompressiveNotLinear() {
        // Halfway up the range should exceed halfway up the bar, or the
        // top of the range is wasted on shouting.
        XCTAssertGreaterThan(value(Int(ceiling / 2)), 0.6)
    }

    // MARK: - The adaptive ceiling

    /// A sound louder than anything before it must not clip while the
    /// scale catches up.
    func testTheCeilingRisesImmediately() {
        let raised = ActivityMeter.nextCeiling(current: 200, level: 5000)
        XCTAssertEqual(raised, 5000)
        XCTAssertEqual(
            ActivityMeter.normalise(level: 5000, ceiling: raised), 1.0, accuracy: 0.0001)
    }

    /// …and must not fall as fast as it rises, or the scale pumps between
    /// every word.
    func testTheCeilingFallsSlowly() {
        var current: CGFloat = 5000
        for _ in 0..<10 { current = ActivityMeter.nextCeiling(current: current, level: 0) }
        XCTAssertGreaterThan(current, 4000, "the scale collapsed within a fifth of a second")
        XCTAssertLessThan(current, 5000, "the scale never came down at all")
    }

    func testTheCeilingNeverFallsBelowTheMinimum() {
        var current: CGFloat = 5000
        for _ in 0..<5000 { current = ActivityMeter.nextCeiling(current: current, level: 0) }
        XCTAssertEqual(current, ActivityMeter.minimumCeiling)
    }

    func testAConstantSoundStaysAtFullHeightRatherThanFadingOut() {
        // Auto-gain that tracked both ends would decay a steady sound to
        // nothing, which is wrong: a constant sound is constant.
        var current = ActivityMeter.minimumCeiling
        var last: CGFloat = 0
        for _ in 0..<200 {
            current = ActivityMeter.nextCeiling(current: current, level: 4000)
            last = ActivityMeter.normalise(level: 4000, ceiling: current)
        }
        XCTAssertEqual(last, 1.0, accuracy: 0.0001)
    }

    // MARK: - Ballistics

    /// Instant attack. Lagging behind a loud sound is what makes a meter
    /// feel unresponsive, and it is the one thing people notice.
    func testABarJumpsStraightUpToALoudSound() {
        XCTAssertEqual(ActivityMeter.nextHeight(current: 0.1, target: 0.9), 0.9)
    }

    /// Gradual release. A bar that fell as fast as it rose would flicker
    /// at every syllable boundary rather than settle.
    func testABarFallsGraduallyRatherThanDropping() {
        let fallen = ActivityMeter.nextHeight(current: 1.0, target: 0)
        XCTAssertGreaterThan(fallen, 0.8, "the bar dropped instead of easing down")
        XCTAssertLessThan(fallen, 1.0, "the bar never came down at all")
    }

    func testABarReachesSilenceEventually() {
        var height: CGFloat = 1.0
        // A quarter of a second of updates.
        for _ in 0..<50 { height = ActivityMeter.nextHeight(current: height, target: 0) }
        XCTAssertLessThan(height, 0.1, "the meter is still moving long after the sound stopped")
    }

    func testHoldingALevelKeepsTheBarThere() {
        var height: CGFloat = 0
        for _ in 0..<20 { height = ActivityMeter.nextHeight(current: height, target: 0.6) }
        XCTAssertEqual(height, 0.6, accuracy: 0.0001)
    }
}

// MARK: - Symmetry (the shape, and that both halves match)

extension ActivityMeterTests {

    /// The requirement, stated as an assertion: the two halves move
    /// together. Mirrored pairs are the *same computation*, not two that
    /// happen to agree, so this cannot drift.
    func testTheEnvelopeIsExactlyMirrored() {
        for count in [8, 12, 16, 17] {
            for bar in 0..<count {
                XCTAssertEqual(
                    ActivityMeter.envelope(bar: bar, of: count),
                    ActivityMeter.envelope(bar: count - 1 - bar, of: count),
                    accuracy: 0.000001,
                    "bar \(bar) of \(count) does not match its mirror")
            }
        }
    }

    /// Speed is mirrored too — otherwise one half would swell ahead of the
    /// other and the symmetry of the outline would not survive motion.
    func testResponsivenessIsExactlyMirrored() {
        for count in [8, 12, 16, 17] {
            for bar in 0..<count {
                XCTAssertEqual(
                    ActivityMeter.responsiveness(bar: bar, of: count),
                    ActivityMeter.responsiveness(bar: count - 1 - bar, of: count),
                    accuracy: 0.000001)
            }
        }
    }

    func testTheCentreIsTallestAndTheEndsAreShortest() throws {
        let count = 16
        let heights = (0..<count).map { ActivityMeter.envelope(bar: $0, of: count) }
        let tallest = try XCTUnwrap(heights.max())
        let shortest = try XCTUnwrap(heights.min())
        XCTAssertEqual(tallest, heights[count / 2], accuracy: 0.05)
        XCTAssertEqual(shortest, heights[0], accuracy: 0.000001)
    }

    /// The ends stay dots. They have to move a little — a bar frozen at
    /// silence while its neighbours dance looks broken — but only a
    /// little, or the row scales as one object instead of changing shape.
    func testTheEndsStayNearlyDots() {
        let ends = ActivityMeter.envelope(bar: 0, of: 9)
        XCTAssertGreaterThan(ends, 0.05, "the ends never move at all")
        XCTAssertLessThan(ends, 0.25, "the ends rise too far to read as dots")
    }

    /// The variation the shape depends on: bars do not share ballistics.
    /// With one rate for all of them the row rose and fell as a single
    /// object, which is what "they all go up and come down" describes.
    func testBarsFallAtDifferentRates() {
        let centre = ActivityMeter.releaseRate(bar: 4, of: 9)
        let edge = ActivityMeter.releaseRate(bar: 0, of: 9)
        XCTAssertLessThan(centre, edge, "the centre should drop away faster than the ends")
        XCTAssertGreaterThan(edge, 0.9, "the ends fall so fast they cannot linger")
        XCTAssertLessThan(edge, 1.0, "the ends never come down at all")
    }

    func testReleaseIsExactlyMirrored() {
        for count in [8, 9, 12, 17] {
            for bar in 0..<count {
                XCTAssertEqual(
                    ActivityMeter.releaseRate(bar: bar, of: count),
                    ActivityMeter.releaseRate(bar: count - 1 - bar, of: count),
                    accuracy: 0.000001)
            }
        }
    }

    /// The visible consequence, checked end to end: after a loud moment
    /// stops, the ends are still up while the centre has already dropped.
    func testAfterASoundTheEndsAreStillFallingWhenTheCentreHasGone() {
        var centre: CGFloat = 1
        var edge: CGFloat = 1
        for _ in 0..<15 {
            centre = ActivityMeter.nextHeight(
                current: centre, target: 0, release: ActivityMeter.releaseRate(bar: 4, of: 9))
            edge = ActivityMeter.nextHeight(
                current: edge, target: 0, release: ActivityMeter.releaseRate(bar: 0, of: 9))
        }
        XCTAssertLessThan(centre, edge, "the row is draining uniformly — no shape change")
    }

    func testTheCentreRespondsFasterThanTheEnds() {
        // The one thing stopping this looking like a rectangle stretching.
        let centre = ActivityMeter.responsiveness(bar: 8, of: 16)
        let edge = ActivityMeter.responsiveness(bar: 0, of: 16)
        XCTAssertGreaterThan(centre, edge)
        XCTAssertGreaterThan(edge, 0.15, "the ends lag so far behind they look disconnected")
    }

    /// A slower bar still arrives — a lag that never resolves would leave
    /// the ends permanently short of the shape.
    func testASlowBarStillReachesItsTarget() {
        var height: CGFloat = 0
        let attack = ActivityMeter.responsiveness(bar: 0, of: 16)
        for _ in 0..<40 {
            height = ActivityMeter.nextHeight(current: height, target: 0.8, attack: attack)
        }
        XCTAssertEqual(height, 0.8, accuracy: 0.01)
    }

    func testASingleBarIsAlwaysFullHeight() {
        XCTAssertEqual(ActivityMeter.envelope(bar: 0, of: 1), 1)
        XCTAssertEqual(ActivityMeter.responsiveness(bar: 0, of: 1), 1)
    }

    /// A quiet room must be still. Ballistics that varied per bar could
    /// easily leave the ends twitching after everything else settled.
    func testEveryBarSettlesToSilence() {
        for bar in 0..<9 {
            var height: CGFloat = 1
            for _ in 0..<200 {
                height = ActivityMeter.nextHeight(
                    current: height, target: 0,
                    release: ActivityMeter.releaseRate(bar: bar, of: 9))
            }
            XCTAssertLessThan(height, 0.02, "bar \(bar) is still moving long after silence")
        }
    }
}

// MARK: - Contrast (the panel is black; the bars must be seen against it)

extension ActivityMeterTests {

    /// One colour for "recording", used in both places it is shown.
    /// Restating the hex here instead would let the menu-bar mark and the
    /// panel drift into two different oranges.
    func testBarsUseTheSameBrandColourAsTheMenuBarMark() {
        XCTAssertEqual(ActivityMeter.barColor, StatusIcon.brandColor)
    }

    /// White on translucent grey was barely visible. Contrast against
    /// black is what makes the meter readable at a glance, which is the
    /// panel's entire job.
    func testBarsContrastStronglyWithTheBlackPanel() {
        let bars = ActivityMeter.barColor.usingColorSpace(.sRGB)!
        // WCAG relative luminance.
        func channel(_ c: CGFloat) -> CGFloat {
            c <= 0.03928 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4)
        }
        let luminance =
            0.2126 * channel(bars.redComponent)
            + 0.7152 * channel(bars.greenComponent)
            + 0.0722 * channel(bars.blueComponent)
        let ratio = (luminance + 0.05) / 0.05
        XCTAssertGreaterThan(ratio, 4.5, "the bars would not stand out against the panel")
    }
}
