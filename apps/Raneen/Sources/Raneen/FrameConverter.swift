import AVFoundation

/// Converts hardware audio buffers to the one format the core accepts.
///
/// The core speaks **PCM16, mono, 16 kHz** and nothing else — the VAD
/// splits frames into whole 20 ms sub-frames and openWakeWord requires
/// exactly 1280 samples. Microphones do not oblige: the built-in runs at
/// 48 kHz float32, AirPods at 24 kHz. Something has to convert, and
/// ROADMAP AD-16 puts that here rather than in Python, because
/// `AVAudioConverter` is Apple's own resampler and 24 kHz → 16 kHz is a
/// 2:3 ratio whose naive form aliases — inaudibly to a human, audibly to
/// Whisper.
///
/// ## Two things that are easy to get wrong
///
/// **The converter is stateful and must be reused.** A resampling
/// `AVAudioConverter` carries filter history between calls. Building a
/// fresh one per buffer discards that history 12 times a second, which
/// puts a discontinuity at every frame boundary. So one instance lives
/// for as long as the input format does.
///
/// **Output size is not input size.** Downsampling 48 kHz → 16 kHz emits
/// roughly a third as many frames, but not exactly: the converter holds
/// samples back and may return more or fewer than the ratio predicts. The
/// output buffer is therefore over-allocated and the *reported*
/// `frameLength` is what gets read — never the capacity.
///
/// Emitting whatever size falls out is deliberate. `PipeAudioSource` on
/// the Python side re-blocks to 1280, and it needs that buffer regardless
/// because a socket read never aligns to frame boundaries. Making every
/// native shell re-block too would be work in three places to save none.
final class FrameConverter {

    /// What the core requires. Interleaved is irrelevant at one channel
    /// but must be stated, or `AVAudioFormat` picks the deinterleaved
    /// layout and the converter silently produces a format mismatch.
    static let target = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: 16000,
        channels: 1,
        interleaved: true
    )!

    let inputFormat: AVAudioFormat
    private let converter: AVAudioConverter

    /// Fails when no conversion path exists between the formats, which in
    /// practice means an input format Core Audio invented and cannot read.
    init?(from inputFormat: AVAudioFormat) {
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0 else { return nil }
        guard let converter = AVAudioConverter(from: inputFormat, to: Self.target) else {
            return nil
        }
        self.inputFormat = inputFormat
        self.converter = converter
    }

    /// Convert one hardware buffer. Returns nil if it yielded no samples.
    ///
    /// A nil return is normal, not an error: a resampler accumulating
    /// input can legitimately produce nothing from a short buffer.
    func convert(_ buffer: AVAudioPCMBuffer) -> Data? {
        guard buffer.frameLength > 0 else { return nil }

        let ratio = Self.target.sampleRate / inputFormat.sampleRate
        // Over-allocate: the converter's own buffering means output can
        // exceed the ratio for a given call. +1024 rather than a tight
        // bound because a too-small buffer costs a dropped frame, while
        // slack costs a few KB that is reused immediately.
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
        guard let out = AVAudioPCMBuffer(pcmFormat: Self.target, frameCapacity: capacity) else {
            return nil
        }

        var consumed = false
        var conversionError: NSError?
        let status = converter.convert(to: out, error: &conversionError) { _, inputStatus in
            // The callback is asked repeatedly until it declines. Handing
            // the same buffer over twice would duplicate audio.
            if consumed {
                inputStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            inputStatus.pointee = .haveData
            return buffer
        }

        guard status != .error, out.frameLength > 0 else { return nil }
        guard let samples = out.int16ChannelData else { return nil }

        return Data(bytes: samples[0], count: Int(out.frameLength) * MemoryLayout<Int16>.size)
    }
}
