import Foundation

/// The silence the shell writes before closing the microphone.
///
/// **A consumer decides that speech has ended by seeing silence, and a
/// closed microphone shows it nothing.** The speaker consumer settles its
/// identification — the only answer trusted to teach or create a profile —
/// on its detector's `Stopped`, which needs `silence_frames` consecutive
/// silent frames. Under AD-23 the device closes the moment the core
/// confirms the turn is over, so without this the detector never sees
/// those frames, never stops, and "Add a person" never completes. It
/// showed up as identification that worked only for turns longer than the
/// re-identification interval, and then only provisionally.
///
/// So the shell writes the frames itself: the core's silence threshold
/// plus a small margin, all zeros, in one write. This is not a trick on
/// the core. A closed microphone *is* silence, and the byte stream is the
/// shell's to shape (AD-16); the tail says in bytes what the disarm said
/// in a command. It is not paced in real time — the bus is a ring buffer
/// and ten frames are 25 KB — and it is sent whether or not speaker
/// identification is on, because any consumer with a detector would fall
/// off the same cliff.
enum SilenceTail {

    /// Fixed by the audio contract: PCM16, 16 kHz, 1280 samples a frame.
    static let samplesPerFrame = 1280
    static let bytesPerSample = 2
    static let bytesPerFrame = samplesPerFrame * bytesPerSample

    /// Frames beyond the threshold. The detector reports `Stopped` on the
    /// frame that *reaches* the threshold, so one would do; two covers a
    /// frame the core's re-blocking may still be holding from the live
    /// stream.
    static let margin = 2

    /// How many silent frames to write for a core running with this
    /// silence threshold.
    static func frames(silenceFrames: Int) -> Int {
        max(silenceFrames, 0) + margin
    }

    /// The bytes to write: every sample zero.
    static func data(silenceFrames: Int) -> Data {
        Data(count: frames(silenceFrames: silenceFrames) * bytesPerFrame)
    }
}
