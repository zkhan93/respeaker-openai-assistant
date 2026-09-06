import XCTest

@testable import Raneen

/// The silence written before the microphone closes.
///
/// Pinned because the failure it prevents is silent in both senses: too
/// short and the speaker consumer never settles, and nothing reports that
/// — identification simply stops teaching.
final class SilenceTailTests: XCTestCase {

    /// The core stops after `silence_frames` consecutive silent frames, so
    /// the tail must be at least that long, and it is a little longer.
    func testTheTailOutlastsTheCoreSSilenceThreshold() {
        for threshold in [8, 25, 100] {
            XCTAssertGreaterThan(SilenceTail.frames(silenceFrames: threshold), threshold)
        }
    }

    /// The dictation default is 8 frames; the tail for it is 10.
    func testTheDictationDefaultProducesTenFrames() {
        XCTAssertEqual(SilenceTail.frames(silenceFrames: 8), 10)
    }

    /// One frame is 1280 samples of PCM16, fixed by the contract — a tail
    /// of any other framing would be re-blocked by the core into frames
    /// with a ragged end, and a half frame of zeros is not a frame.
    func testTheTailIsWholeFramesOfZeros() {
        let data = SilenceTail.data(silenceFrames: 8)
        XCTAssertEqual(data.count, 10 * 1280 * 2)
        XCTAssertEqual(data.count % SilenceTail.bytesPerFrame, 0)
        XCTAssertTrue(data.allSatisfy { $0 == 0 })
    }

    /// A nonsense threshold still yields a tail rather than a crash.
    func testANegativeThresholdStillYieldsTheMargin() {
        XCTAssertEqual(SilenceTail.frames(silenceFrames: -5), SilenceTail.margin)
    }
}
