import AVFoundation

/// The short tones that say dictation turned on or off.
///
/// A Swift port of `voice_desktop/adapters/earcon_indicator.py`, kept
/// deliberately identical in pitch and timing so the CLI and the app
/// sound like one product. The design reasoning lives in ROADMAP AD-13
/// and is not repeated here — only what is specific to doing it natively.
///
/// ## Why this moved out of Python (AD-16)
///
/// The helper picked its output device once, at startup, and held the
/// stream open. Connect AirPods mid-session and the beep kept going to
/// the laptop speakers — playing into a device nobody is listening to,
/// with no error, forever. `AVAudioEngine` follows the system default
/// output instead, and this rebuilds itself when that default moves, so
/// the sound always arrives wherever you are actually listening.
struct Earcon {

    /// Tone frequencies in Hz, played in order. Two conveys direction —
    /// rising reads as "on", falling as "off" — which is the whole
    /// vocabulary needed.
    let freqs: [Double]
    var toneSeconds: Double = 0.055

    /// Raised-cosine fade at each end of each tone. **Not optional**: a
    /// sine starting at full amplitude is a step discontinuity, and the
    /// click is louder than the tone itself.
    var fadeSeconds: Double = 0.006

    /// Rising fifth for "on", falling for "off".
    ///
    /// Pitched above conversational speech and kept very short, because
    /// under hold-to-talk this plays into a live microphone — and now
    /// more so than before, since the beep and the mic may both be the
    /// same pair of AirPods.
    static let rising = Earcon(freqs: [880, 1320])
    static let falling = Earcon(freqs: [1320, 880])

    /// Something failed — a repeated low tone, deliberately unlike the
    /// two musical ones so it cannot be mistaken for normal operation.
    static let error = Earcon(freqs: [320, 320], toneSeconds: 0.07)

    /// Which indicator patterns make a sound.
    ///
    /// The arming layer plus errors. `listen`/`think`/`off` cycle once
    /// per utterance, so sounding them would beep after every sentence.
    static func forPattern(_ pattern: String) -> Earcon? {
        switch pattern {
        case "armed": return .rising
        case "disarmed": return .falling
        case "error": return .error
        default: return nil
        }
    }

    /// Render to a float buffer at `format`'s rate.
    func render(format: AVAudioFormat, volume: Double) -> AVAudioPCMBuffer? {
        let rate = format.sampleRate
        let perTone = max(1, Int(rate * toneSeconds))
        let fade = max(1, Int(rate * fadeSeconds))
        let total = perTone * freqs.count
        let peak = Float(min(max(volume, 0), 1))

        guard
            let buffer = AVAudioPCMBuffer(
                pcmFormat: format, frameCapacity: AVAudioFrameCount(total))
        else { return nil }
        buffer.frameLength = AVAudioFrameCount(total)
        guard let channel = buffer.floatChannelData?[0] else { return nil }

        var index = 0
        for freq in freqs {
            for i in 0..<perTone {
                // Raised cosine in, raised cosine out, flat between.
                let envelope: Double
                if i < fade {
                    envelope = 0.5 - 0.5 * cos(.pi * Double(i) / Double(fade))
                } else if i > perTone - fade {
                    envelope = 0.5 - 0.5 * cos(.pi * Double(perTone - i) / Double(fade))
                } else {
                    envelope = 1.0
                }
                let sample = sin(2 * .pi * freq * Double(i) / rate)
                channel[index] = Float(sample * envelope) * peak
                index += 1
            }
        }
        return buffer
    }
}

/// Plays earcons on whichever output device is currently active.
///
/// Rebuilt on `AVAudioEngineConfigurationChange` rather than pinned at
/// startup — that is the entire point. Buffers are re-rendered when the
/// rate changes, since a 22.05 kHz buffer played through a 48 kHz engine
/// is a chipmunk.
final class EarconPlayer {

    /// Matches the CLI's default. Quiet on purpose: under hold-to-talk
    /// this lands inside the recorded audio.
    static let defaultVolume = 0.15

    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var format: AVAudioFormat?
    private let lock = NSLock()
    private var ready = false
    private let volume: Double

    init(volume: Double = EarconPlayer.defaultVolume) {
        self.volume = volume
    }

    /// Open the output device now, so the first beep is not late.
    ///
    /// Failure is not fatal and not even reported to the user: losing the
    /// beep is a downgrade, not a breakage, and the icon and panel still
    /// say what is happening.
    func prepare() {
        lock.lock()
        defer { lock.unlock() }
        guard !ready else { return }

        // The engine's own output format — whatever the active device
        // wants. Rendering to match avoids a resample on every beep.
        let output = engine.outputNode.outputFormat(forBus: 0)
        guard output.sampleRate > 0,
            let mono = AVAudioFormat(
                commonFormat: .pcmFormatFloat32,
                sampleRate: output.sampleRate,
                channels: 1,
                interleaved: false)
        else { return }

        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: mono)
        engine.prepare()
        do {
            try engine.start()
        } catch {
            NSLog("earcons unavailable: %@", "\(error)")
            return
        }
        player.play()
        format = mono
        ready = true

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(outputChanged),
            name: .AVAudioEngineConfigurationChange,
            object: engine
        )
    }

    func play(_ earcon: Earcon) {
        lock.lock()
        let format = self.format
        let ready = self.ready
        lock.unlock()

        guard ready, let format, let buffer = earcon.render(format: format, volume: volume) else {
            return
        }
        // Fire and forget. Scheduling is cheap and asynchronous, so this
        // is safe to call from the main thread on a hotkey press.
        player.scheduleBuffer(buffer, at: nil, options: [], completionHandler: nil)
    }

    /// The output device moved. Rebuild against the new one.
    @objc private func outputChanged(_ notification: Notification) {
        NSLog("earcon output device changed — reopening")
        teardown()
        // A moment for the new device to settle; a Bluetooth handoff is
        // not instant, and reopening too early just fails.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            self?.prepare()
        }
    }

    private func teardown() {
        lock.lock()
        let wasReady = ready
        ready = false
        format = nil
        lock.unlock()
        guard wasReady else { return }

        NotificationCenter.default.removeObserver(
            self, name: .AVAudioEngineConfigurationChange, object: engine)
        player.stop()
        engine.stop()
        engine.detach(player)
    }

    func close() { teardown() }

    deinit { teardown() }
}
