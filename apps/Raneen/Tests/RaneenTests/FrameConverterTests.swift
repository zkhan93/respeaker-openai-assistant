import AVFoundation
import XCTest

@testable import Raneen

/// Format conversion, which AD-16 named as the one genuinely new and
/// fiddly piece of moving capture into the native layer.
///
/// Runs with no microphone: every buffer here is synthesized, so these
/// exercise the converter rather than the machine's audio hardware.
final class FrameConverterTests: XCTestCase {

    /// A tone in whatever format the hardware might hand us.
    private func hardwareBuffer(
        rate: Double,
        channels: AVAudioChannelCount = 1,
        frames: AVAudioFrameCount
    ) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: rate,
            channels: channels,
            interleaved: false
        )!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
        buffer.frameLength = frames
        for channel in 0..<Int(channels) {
            let samples = buffer.floatChannelData![channel]
            for i in 0..<Int(frames) {
                samples[i] = sin(2 * .pi * 440 * Float(i) / Float(rate)) * 0.5
            }
        }
        return buffer
    }

    // MARK: - The contract with the core

    func testTargetIsExactlyWhatTheCoreRequires() {
        // The VAD sub-frames at 20 ms and openWakeWord demands 1280
        // samples; neither survives a different rate or channel count.
        let target = FrameConverter.target
        XCTAssertEqual(target.sampleRate, 16000)
        XCTAssertEqual(target.channelCount, 1)
        XCTAssertEqual(target.commonFormat, .pcmFormatInt16)
    }

    func testTargetIsInterleaved() {
        // Irrelevant at one channel, but if it is left unstated
        // AVAudioFormat picks the deinterleaved layout and int16ChannelData
        // stops meaning what this code assumes.
        XCTAssertTrue(FrameConverter.target.isInterleaved)
    }

    // MARK: - The formats real hardware produces

    /// Samples produced from `count` buffers of 100 ms each.
    ///
    /// **Measured in aggregate on purpose.** A single call does not return
    /// `frames × ratio`: the converter accumulates input and emits on its
    /// own schedule, so one 100 ms buffer at 48 kHz yields 1365 samples,
    /// not the 1600 arithmetic predicts, and a later one yields more. Only
    /// the total tracks the ratio — which is precisely why the far side
    /// re-blocks and nothing here promises a frame size.
    private func samples(
        fromBuffersAt rate: Double, channels: AVAudioChannelCount = 1, count: Int
    ) -> Int {
        let format = hardwareBuffer(rate: rate, channels: channels, frames: 1).format
        guard let converter = FrameConverter(from: format) else {
            XCTFail("no converter for \(rate) Hz / \(channels) ch")
            return 0
        }
        let frames = AVAudioFrameCount(rate / 10)  // 100 ms
        var total = 0
        for _ in 0..<count {
            let buffer = hardwareBuffer(rate: rate, channels: channels, frames: frames)
            total += (converter.convert(buffer)?.count ?? 0) / 2
        }
        return total
    }

    /// The converter is never more than this far behind real time.
    ///
    /// Measured: the shortfall sits between ~590 and ~1160 samples and
    /// does **not** grow with duration (see
    /// `testTheShortfallIsLatencyNotLostAudio`), so it is audio still
    /// inside the converter rather than audio thrown away. 2000 leaves
    /// room above the observed spread without being so loose that real
    /// loss would slip through.
    private static let maxInFlightSamples = 2000

    /// Output must account for every input sample bar what is in flight.
    ///
    /// Deliberately an absolute bound rather than a percentage: a
    /// percentage passes whether the deficit is fixed latency or steady
    /// loss, and those are opposite verdicts. Anything proportional fails
    /// this as soon as the clip is long enough.
    private func assertAccountsForAllAudio(
        _ total: Int, seconds: Double, _ message: String = "", line: UInt = #line
    ) {
        let expected = Int(seconds * 16000)
        let deficit = expected - total
        XCTAssertGreaterThanOrEqual(
            deficit, 0, "produced more audio than went in. \(message)", line: line)
        XCTAssertLessThanOrEqual(
            deficit, Self.maxInFlightSamples,
            "\(deficit) samples missing from \(expected) — audio is being dropped, "
                + "not buffered. \(message)",
            line: line)
    }

    /// 48 kHz float32 is what a MacBook's input node delivers.
    func testTheBuiltInMicrophoneFormatConverts() {
        assertAccountsForAllAudio(samples(fromBuffersAt: 48000, count: 20), seconds: 2)
    }

    /// AirPods run at 24 kHz — a 2:3 ratio, not clean decimation, which
    /// is exactly why this is Apple's resampler and not one of ours.
    func testTheAirPodsFormatConverts() {
        assertAccountsForAllAudio(samples(fromBuffersAt: 24000, count: 20), seconds: 2)
    }

    func testStereoInputIsMixedToMono() {
        assertAccountsForAllAudio(
            samples(fromBuffersAt: 48000, channels: 2, count: 20), seconds: 2,
            "stereo should still yield exactly one mono stream")
    }

    /// The documented surprise, pinned so nobody "fixes" it.
    ///
    /// A single call returns whatever the converter has ready, not
    /// `frames × ratio`. Code that assumed otherwise — sizing a buffer
    /// from the ratio, or asserting a frame count — would drop audio
    /// intermittently, which is far harder to spot than dropping it
    /// always.
    func testOneCallDoesNotReturnTheRatioWouldPredict() {
        let converter = FrameConverter(from: hardwareBuffer(rate: 48000, frames: 1).format)!
        let first = (converter.convert(hardwareBuffer(rate: 48000, frames: 4800))?.count ?? 0) / 2
        XCTAssertGreaterThan(first, 0)
        XCTAssertNotEqual(first, 1600, "if this now holds, the aggregate tests can tighten")
    }

    func testAMatchingFormatStillWorks() {
        // A host device already at 16 kHz mono is a no-op conversion, and
        // must not be treated as an error.
        let format = AVAudioFormat(
            commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
        XCTAssertNotNil(FrameConverter(from: format))
    }

    // MARK: - Output is whole PCM16 samples

    func testOutputIsAWholeNumberOfInt16Samples() {
        let converter = FrameConverter(from: hardwareBuffer(rate: 48000, frames: 1).format)!
        for frames in [512, 1024, 4096, 7999] as [AVAudioFrameCount] {
            let data = converter.convert(hardwareBuffer(rate: 48000, frames: frames))
            if let data {
                XCTAssertEqual(data.count % 2, 0, "\(frames) frames produced half a sample")
            }
        }
    }

    func testConvertedAudioIsNotSilence() {
        let converter = FrameConverter(from: hardwareBuffer(rate: 48000, frames: 1).format)!
        let data = converter.convert(hardwareBuffer(rate: 48000, frames: 4800))!
        XCTAssertTrue(data.contains { $0 != 0 }, "a 440 Hz tone converted to pure silence")
    }

    // MARK: - Statefulness

    /// The converter carries resampling history, so one instance must
    /// serve every buffer. Rebuilding it per call would drop that history
    /// twelve times a second and put a discontinuity at each boundary.
    ///
    /// Checked by volume: across many buffers the total output must track
    /// the ratio. A converter losing state would drift.
    func testReusingOneConverterKeepsTheStreamContinuous() {
        let converter = FrameConverter(from: hardwareBuffer(rate: 48000, frames: 1).format)!
        var total = 0
        for _ in 0..<20 {
            total += (converter.convert(hardwareBuffer(rate: 48000, frames: 4800))?.count ?? 0) / 2
        }
        assertAccountsForAllAudio(total, seconds: 2)
    }

    /// The distinction the absolute bound exists to make.
    ///
    /// A converter holding a fixed amount in flight is fine — a constant
    /// ~65 ms of latency nobody notices. A converter dropping a *fraction*
    /// of its input is a bug that gets worse the longer you dictate, and
    /// the two are indistinguishable from a single measurement.
    func testTheShortfallIsLatencyNotLostAudio() {
        let brief = 16000 * 2 - samples(fromBuffersAt: 48000, count: 20)   // 2 s
        let long = 16000 * 16 - samples(fromBuffersAt: 48000, count: 160)  // 16 s

        // Proportional loss would make the 16 s deficit roughly 8x the 2 s
        // one. It stays flat instead, so nothing is being thrown away.
        XCTAssertLessThan(
            long, brief * 4,
            "the deficit grew with duration (\(brief) over 2 s, \(long) over 16 s) — "
                + "that is dropped audio, not buffering")
    }

    // MARK: - Degenerate input

    func testAnEmptyBufferYieldsNothingRatherThanCrashing() {
        let converter = FrameConverter(from: hardwareBuffer(rate: 48000, frames: 1).format)!
        let empty = AVAudioPCMBuffer(
            pcmFormat: hardwareBuffer(rate: 48000, frames: 1).format, frameCapacity: 512)!
        empty.frameLength = 0
        XCTAssertNil(converter.convert(empty))
    }

    func testAZeroRateFormatIsRejected() {
        // What `inputNode.inputFormat` reports when there is no usable
        // microphone — better caught here than as a divide by zero.
        let format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32, sampleRate: 8000, channels: 1, interleaved: false)!
        XCTAssertNotNil(FrameConverter(from: format), "8 kHz is unusual but legal")
    }
}
