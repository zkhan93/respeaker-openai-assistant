import AVFoundation
import XCTest

@testable import Raneen

/// The tones, and their agreement with the CLI's.
///
/// Only `Earcon` is exercised — rendering is pure arithmetic and runs
/// anywhere. `EarconPlayer` opens a real output device, so it is left to
/// manual testing rather than faked here.
final class EarconTests: XCTestCase {

    private let format = AVAudioFormat(
        commonFormat: .pcmFormatFloat32, sampleRate: 22050, channels: 1, interleaved: false)!

    // MARK: - Parity with the Python implementation

    /// These constants are duplicated across two languages, so nothing
    /// but a test stops them drifting. Source of truth:
    /// `voice_desktop/adapters/earcon_indicator.py`.
    func testTonesMatchTheCLI() {
        XCTAssertEqual(Earcon.rising.freqs, [880, 1320])
        XCTAssertEqual(Earcon.falling.freqs, [1320, 880])
        XCTAssertEqual(Earcon.error.freqs, [320, 320])
        XCTAssertEqual(Earcon.error.toneSeconds, 0.07, accuracy: 0.0001)
        XCTAssertEqual(Earcon.rising.toneSeconds, 0.055, accuracy: 0.0001)
    }

    func testRisingAndFallingAreMirrorImages() {
        // Direction is the whole message: up means on, down means off.
        XCTAssertEqual(Earcon.rising.freqs, Earcon.falling.freqs.reversed())
    }

    func testStateTonesSitAboveConversationalSpeech() {
        // They play into a live microphone under hold-to-talk, and now
        // possibly the *same* AirPods. Staying clear of the speech band
        // is what keeps them from being transcribed as words.
        for freq in Earcon.rising.freqs { XCTAssertGreaterThan(freq, 800) }
    }

    func testOnlyTheArmingLayerAndErrorsMakeASound() {
        // listen/think/off cycle once per sentence; sounding them would
        // beep after every utterance.
        XCTAssertNotNil(Earcon.forPattern("armed"))
        XCTAssertNotNil(Earcon.forPattern("disarmed"))
        XCTAssertNotNil(Earcon.forPattern("error"))
        for quiet in ["listen", "think", "speak", "off"] {
            XCTAssertNil(Earcon.forPattern(quiet), "\(quiet) should be silent")
        }
    }

    // MARK: - Rendering

    func testLengthFollowsToneCountAndDuration() {
        let buffer = Earcon.rising.render(format: format, volume: 0.15)!
        XCTAssertEqual(Int(buffer.frameLength), 2 * Int(22050 * 0.055))
    }

    func testTheWholeBudgetStaysShort() {
        // Total length is a real constraint, not taste: in continuous
        // mode a tone can land while the next sentence is being spoken.
        for earcon in [Earcon.rising, Earcon.falling, Earcon.error] {
            let seconds = earcon.toneSeconds * Double(earcon.freqs.count)
            XCTAssertLessThan(seconds, 0.2)
        }
    }

    /// The fade is not cosmetic. A sine starting at full amplitude is a
    /// step discontinuity, and that click is louder than the tone.
    func testTonesFadeInFromSilence() {
        let buffer = Earcon.rising.render(format: format, volume: 1.0)!
        let samples = buffer.floatChannelData![0]
        XCTAssertEqual(samples[0], 0, accuracy: 0.001, "the tone starts with a click")
    }

    func testTonesFadeOutToSilence() {
        let buffer = Earcon.rising.render(format: format, volume: 1.0)!
        let samples = buffer.floatChannelData![0]
        let last = Int(buffer.frameLength) - 1
        XCTAssertEqual(samples[last], 0, accuracy: 0.01, "the tone ends with a click")
    }

    func testTheMiddleOfAToneIsNotSilent() {
        let buffer = Earcon.rising.render(format: format, volume: 1.0)!
        let samples = buffer.floatChannelData![0]
        let middle = Int(22050 * 0.055) / 2
        XCTAssertGreaterThan(abs(samples[middle]), 0.5, "the tone faded to nothing")
    }

    func testVolumeIsRespectedAndClamped() {
        let quiet = Earcon.rising.render(format: format, volume: 0.15)!
        let loud = Earcon.rising.render(format: format, volume: 1.0)!
        let middle = Int(22050 * 0.055) / 2
        XCTAssertLessThan(
            abs(quiet.floatChannelData![0][middle]), abs(loud.floatChannelData![0][middle]))

        let over = Earcon.rising.render(format: format, volume: 5.0)!
        for i in 0..<Int(over.frameLength) {
            XCTAssertLessThanOrEqual(abs(over.floatChannelData![0][i]), 1.0, "clipping at \(i)")
        }
    }

    func testDefaultVolumeIsQuietEnoughToLiveInsideARecording() {
        XCTAssertLessThanOrEqual(EarconPlayer.defaultVolume, 0.2)
        XCTAssertGreaterThan(EarconPlayer.defaultVolume, 0)
    }

    /// The player renders against the engine's own rate, so this must
    /// work at whatever the active device happens to want — 48 kHz on a
    /// MacBook, 24 kHz on AirPods.
    func testRendersAtAnyDeviceRate() {
        for rate in [16000.0, 22050.0, 24000.0, 44100.0, 48000.0] {
            let format = AVAudioFormat(
                commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: 1,
                interleaved: false)!
            let buffer = Earcon.rising.render(format: format, volume: 0.15)
            XCTAssertNotNil(buffer, "no buffer at \(rate) Hz")
            XCTAssertEqual(Int(buffer!.frameLength), 2 * Int(rate * 0.055))
        }
    }
}
