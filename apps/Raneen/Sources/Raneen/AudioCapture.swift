import AVFoundation

/// The microphone, owned natively.
///
/// This is the point of ROADMAP AD-16. The Python helper used to open the
/// device through PortAudio, which meant working around a library that
/// caches its device list at init, exposes no stable device IDs, and has
/// host-API-dependent behaviour when a device disappears. Core Audio has
/// none of those problems, so capture moved up here and the core became
/// "give me bytes, take back text".
///
/// Frames go out through `onFrames` as PCM16 mono 16 kHz, in whatever
/// sizes the converter produces — `PipeAudioSource` re-blocks to 1280 on
/// the far side. See `FrameConverter` for why that split.
final class AudioCapture {

    /// Requested tap size. Core Audio treats this as a hint and may hand
    /// us something else entirely, which is fine — nothing downstream
    /// depends on it.
    private static let tapBufferSize: AVAudioFrameCount = 4096

    private let engine = AVAudioEngine()
    private var converter: FrameConverter?
    private let lock = NSLock()
    private var running = false

    /// Whether a tap and observer are currently installed.
    ///
    /// Tracked separately from `running` because a configuration change
    /// stops capture without removing either. Keying teardown off
    /// `running` meant a restart skipped it and installed a *second* tap
    /// on the same bus — every frame delivered twice, at double speed.
    private var installed = false

    /// Called on Core Audio's capture thread. Must return promptly: this
    /// is a real-time context and blocking here drops audio.
    var onFrames: ((Data) -> Void)?

    /// Called when capture stops for a reason we did not ask for.
    ///
    /// Device disappearing, engine configuration change, route change. The
    /// caller decides what to do; this type only reports. Without it a
    /// dead microphone is silent in exactly the way a quiet room is.
    var onInterruption: (() -> Void)?

    var isRunning: Bool {
        lock.lock()
        defer { lock.unlock() }
        return running
    }

    /// The format the hardware is actually delivering, for diagnostics.
    var inputFormat: AVAudioFormat? { converter?.inputFormat }

    // MARK: - Lifecycle

    /// Open a microphone and start delivering frames.
    ///
    /// - Parameter device: Which microphone. `nil` follows whatever the
    ///   system default is, which is what "System Default" in the menu
    ///   means. A specific device is pinned until the user says otherwise,
    ///   even when macOS moves its own default elsewhere.
    func start(device: AudioDevice? = nil) throws {
        lock.lock()
        if running {
            lock.unlock()
            return
        }
        lock.unlock()

        // Unconditional, so restarting after a device change cannot leave
        // the previous tap in place. Safe when nothing is installed.
        teardown()

        let input = engine.inputNode

        // Must happen while the engine is stopped, and *before* the format
        // is read — the input node reports the format of whichever device
        // it is currently bound to, so querying first would configure the
        // converter for the device we are about to stop using.
        if let device, let unit = input.audioUnit {
            var id = device.id
            let status = AudioUnitSetProperty(
                unit,
                kAudioOutputUnitProperty_CurrentDevice,
                kAudioUnitScope_Global,
                0,
                &id,
                UInt32(MemoryLayout<AudioDeviceID>.size)
            )
            if status != noErr {
                // Not fatal: falling back to the system default keeps
                // dictation working, which beats refusing to listen
                // because one preference could not be honoured.
                NSLog("could not select input device %@ (%d)", device.name, status)
            }
        }
        // Ask the *node*, not the device: this is the format the tap will
        // deliver, and on a Mac it is usually 48 kHz float32 regardless of
        // what the microphone natively runs at.
        let format = input.inputFormat(forBus: 0)
        guard format.sampleRate > 0 else {
            throw NSError(
                domain: "Raneen.AudioCapture", code: 1,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "the input node reports a zero sample rate, which normally means "
                        + "no microphone is available or access was denied"
                ]
            )
        }
        guard let converter = FrameConverter(from: format) else {
            throw NSError(
                domain: "Raneen.AudioCapture", code: 2,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "cannot convert \(format) to the 16 kHz mono PCM16 the core requires"
                ]
            )
        }
        self.converter = converter

        input.installTap(onBus: 0, bufferSize: Self.tapBufferSize, format: format) {
            [weak self] buffer, _ in
            guard let self, let data = self.converter?.convert(buffer) else { return }
            self.onFrames?(data)
        }

        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            self.converter = nil
            throw error
        }

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(configurationChanged),
            name: .AVAudioEngineConfigurationChange,
            object: engine
        )

        lock.lock()
        running = true
        installed = true
        lock.unlock()

        NSLog(
            "capture started: %.0f Hz %d ch -> 16000 Hz 1 ch int16",
            format.sampleRate, format.channelCount
        )
    }

    func stop() {
        teardown()
    }

    /// Remove the tap and observer and stop the engine. Idempotent.
    ///
    /// Keyed on `installed`, not `running`, so it still cleans up after a
    /// configuration change has already flipped `running` to false.
    private func teardown() {
        lock.lock()
        let wasInstalled = installed
        installed = false
        running = false
        lock.unlock()
        guard wasInstalled else { return }

        NotificationCenter.default.removeObserver(
            self, name: .AVAudioEngineConfigurationChange, object: engine
        )
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        converter = nil
        NSLog("capture stopped")
    }

    /// The engine's configuration changed underneath us.
    ///
    /// Core Audio posts this when the default device changes, a device
    /// disappears, or its format changes. The tap is invalid afterwards
    /// and the engine may have stopped, so this reports rather than trying
    /// to paper over it — recovery policy belongs to the caller, which is
    /// the only thing that knows whether a turn is in progress.
    @objc private func configurationChanged(_ notification: Notification) {
        NSLog("audio configuration changed — capture needs restarting")
        lock.lock()
        running = false
        lock.unlock()
        onInterruption?()
    }

    deinit { stop() }
}
