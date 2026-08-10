import XCTest

@testable import Raneen

/// The level→height maths. The animation itself is visual and only a
/// human can judge it, but an out-of-range level draws bars outside the
/// panel, and a scaling curve that is too flat makes ordinary speech
/// look like silence.
final class WaveformViewTests: XCTestCase {

    private let fullScale = WaveformView.fullScaleLevel

    private func value(_ peak: Int) -> CGFloat {
        WaveformView.normalise(peak: peak, fullScale: fullScale)
    }

    // MARK: - Range

    func testSilenceIsZero() {
        XCTAssertEqual(value(0), 0)
    }

    func testFullScaleIsOne() {
        XCTAssertEqual(value(Int(fullScale)), 1.0, accuracy: 0.0001)
    }

    func testLoudInputIsClamped() {
        // int16 peaks at 32767, four times our full scale.
        XCTAssertEqual(value(32767), 1.0)
    }

    func testNegativeInputDoesNotProduceNegativeBars() {
        XCTAssertEqual(value(-500), 0)
    }

    func testLouderInputAlwaysGivesATallerBar() {
        var previous: CGFloat = -1
        for peak in stride(from: 0, through: Int(fullScale), by: 250) {
            let current = value(peak)
            XCTAssertGreaterThanOrEqual(current, previous)
            previous = current
        }
    }

    // MARK: - The curve

    /// The reason for square-root scaling: linear made ordinary speech
    /// barely move the bars, which read as "not hearing you".
    func testQuietSpeechIsClearlyVisible() {
        // ~900 was a measured level for normal speech at a distance.
        let quiet = value(900)
        XCTAssertGreaterThan(quiet, 0.25, "quiet speech would look like silence")
        XCTAssertLessThan(quiet, 0.5)
    }

    func testTheCurveIsCompressiveNotLinear() {
        // Halfway up the range should exceed halfway up the bar, or the
        // top of the range is wasted on shouting.
        let half = value(Int(fullScale / 2))
        XCTAssertGreaterThan(half, 0.6)
    }

    func testConversationalSpeechUsesMostOfTheRange() {
        // Measured peaks while dictating ran 2.5k–10k.
        XCTAssertGreaterThan(value(2500), 0.5)
        XCTAssertEqual(value(10000), 1.0)
    }
}
