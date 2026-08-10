import AppKit

/// Menu-bar shell for the spike.
///
/// Deliberately minimal: a status item, a hotkey, and a helper process.
/// The point is to prove the three things that packaging can break —
/// that the helper launches from inside a bundle, that the microphone
/// grant reaches a child process, and that an event tap can suppress a
/// key — not to be the real UI.
final class AppDelegate: NSObject, NSApplicationDelegate {

    private var statusItem: NSStatusItem!
    private let hotkey = HotkeyTap()
    private var triggerKey: TriggerKey { hotkey.boundKey }
    private let inserter = TextInserter()
    private lazy var panel = ListeningPanel()
    private var helper: Helper?
    private var audioSocket: AudioSocket?
    private var capture: AudioCapture?

    /// Whether this process owns the microphone (ROADMAP AD-16).
    ///
    /// **Opt-in until it has been run on real hardware.** Everything under
    /// it is unit-tested, but "does Core Audio actually deliver frames
    /// inside a signed bundle" cannot be answered by a test — only by
    /// launching the app. Defaulting to on before that would risk trading
    /// a working dictation tool for an untested one; defaulting to off
    /// costs a single environment variable.
    ///
    /// Turn on with `RANEEN_NATIVE_AUDIO=1`. When off, the helper opens
    /// the microphone through PortAudio exactly as before.
    private static let nativeAudio =
        ProcessInfo.processInfo.environment["RANEEN_NATIVE_AUDIO"] == "1"

    /// Whether a turn is open, mirrored from the helper's state events.
    /// Only used to avoid reopening the microphone mid-sentence.
    private var isArmed = false
    private var restartPending = false
    private var restartAttempts = 0
    private var restartWork: DispatchWorkItem?

    private var lastTranscript = "—"
    private var peak = 0
    private var levelTicks = 0
    private var state = "starting…"

    /// Per-event tracing. Useful while the UI is being built, unbearable
    /// once it ships: at ~6 level events a second this fills a log with
    /// nothing anyone wants. Set RANEEN_DEBUG=1 to turn it on.
    private static let debugLogging =
        ProcessInfo.processInfo.environment["RANEEN_DEBUG"] == "1"

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        show(.starting)
        rebuildMenu()

        startHelper()

        if !hotkey.start() {
            NSLog("hotkey tap FAILED to install — Accessibility not granted")
            state = "no Accessibility permission"
            rebuildMenu()
            promptForAccessibility()
        } else {
            NSLog("hotkey tap installed — %@", self.triggerKey.label)
            hotkey.onPress = { [weak self] in
                if Self.debugLogging { NSLog("hotkey down") }
                self?.helper?.arm()
            }
            hotkey.onRelease = { [weak self] in
                if Self.debugLogging { NSLog("hotkey up") }
                self?.helper?.disarm()
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        hotkey.stop()
        panel.hide()
        // Capture first: stopping the helper closes the far end of the
        // socket, and there is no reason to keep converting audio for a
        // descriptor that is about to disappear.
        capture?.stop()
        helper?.stop()
        audioSocket?.close()
    }

    // MARK: - Helper

    private func startHelper() {
        guard let executable = Self.locateHelper() else {
            state = "helper not found"
            rebuildMenu()
            return
        }

        var arguments = ["serve"]
        if Self.nativeAudio {
            // Listen *before* spawning, so the helper's connect cannot
            // race us. It may sit in accept() for seconds while a Whisper
            // model loads, which is why that happens off the main queue.
            let socket = AudioSocket()
            do {
                try socket.listen()
                socket.acceptInBackground()
                self.audioSocket = socket
                arguments += ["--audio-socket", socket.path, "--no-sound"]
                startCapture(sending: socket)
            } catch {
                NSLog("native audio unavailable (%@) — letting the helper open the mic", "\(error)")
                self.audioSocket = nil
            }
        }

        let helper = Helper(executable: executable, arguments: arguments)
        helper.onEvent = { [weak self] event in
            // Events arrive on a background queue.
            DispatchQueue.main.async { self?.handle(event) }
        }
        do {
            try helper.start()
            self.helper = helper
        } catch {
            state = "helper failed: \(error.localizedDescription)"
            rebuildMenu()
        }
    }

    /// Open the microphone here and stream frames down to the helper.
    ///
    /// Failure is survivable and deliberately not fatal: the menu bar says
    /// so and the app keeps running, because a dictation tool that refuses
    /// to launch is worse than one that cannot hear yet — the usual cause
    /// is a microphone grant the user has not given, which they can fix
    /// without restarting.
    private func startCapture(sending socket: AudioSocket) {
        let capture = AudioCapture()
        capture.onFrames = { [weak socket] data in
            // Core Audio's capture thread. `send` never blocks on a
            // missing reader — it drops, which is right for audio.
            socket?.send(data)
        }
        capture.onInterruption = { [weak self] in
            // A device appeared, vanished, or the system default moved —
            // plugging in AirPods is the ordinary case, not an error.
            DispatchQueue.main.async { self?.audioDeviceChanged() }
        }
        do {
            try capture.start()
            self.capture = capture
        } catch {
            NSLog("could not start capture: %@", "\(error)")
            state = "microphone unavailable"
        }
    }

    // MARK: - Following the audio device

    /// How long to wait before reopening after a configuration change.
    ///
    /// Core Audio posts several notifications for one device change —
    /// connecting AirPods moves the default input *and* the default
    /// output, and each is its own event. Reopening on the first would
    /// mean reopening again on the next two.
    private static let restartDebounce: TimeInterval = 0.4

    /// Give up after this many consecutive failures and say so, rather
    /// than reopening a device that is not coming back, forever.
    private static let maxRestartAttempts = 5

    /// A device changed. Follow it — unless a sentence is in flight.
    private func audioDeviceChanged() {
        guard audioSocket != nil else { return }

        if isArmed {
            // Reopening mid-utterance would cut the recording in half.
            // The key is still down and the old device is still feeding
            // us, so the honest move is to finish the sentence first.
            NSLog("audio device changed while armed — deferring until the key is released")
            restartPending = true
            return
        }
        scheduleCaptureRestart()
    }

    private func scheduleCaptureRestart() {
        restartPending = false
        restartWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.restartCapture() }
        restartWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.restartDebounce, execute: work)
    }

    private func restartCapture() {
        guard let socket = audioSocket else { return }

        capture?.stop()
        capture = nil
        startCapture(sending: socket)

        if capture != nil {
            restartAttempts = 0
            state = "ready"
            show(.idle)
            if let format = capture?.inputFormat {
                NSLog("following the new device: %.0f Hz %d ch", format.sampleRate, format.channelCount)
            }
        } else {
            restartAttempts += 1
            if restartAttempts >= Self.maxRestartAttempts {
                state = "microphone unavailable"
                show(.error)
            } else {
                // Back off rather than hammering a device that is still
                // settling — a Bluetooth handoff takes a moment.
                let delay = Self.restartDebounce * Double(restartAttempts * 2)
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                    self?.restartCapture()
                }
            }
        }
        rebuildMenu()
    }

    /// Where the Python core lives.
    ///
    /// Inside a bundle it sits in `Contents/Resources/helper/`. During
    /// development there is no bundle, so fall back to an explicit path
    /// in the environment — that is what `make run` sets.
    private static func locateHelper() -> URL? {
        if let override = ProcessInfo.processInfo.environment["RANEEN_HELPER"] {
            return URL(fileURLWithPath: override)
        }
        if let resources = Bundle.main.resourceURL {
            let bundled = resources.appendingPathComponent("helper/voice-desktop")
            if FileManager.default.isExecutableFile(atPath: bundled.path) {
                return bundled
            }
        }
        return nil
    }

    private func handle(_ event: Helper.Event) {
        switch event {
        case .ready(let engine, let model):
            state = "ready — \(engine) / \(model)"
            show(.idle)
        case .state(let pattern):
            state = pattern
            show(StatusIcon.forPattern(pattern))
            // The panel exists to answer "is it listening?" — so it
            // tracks arming, not the per-utterance cycle.
            switch pattern {
            case "armed":
                isArmed = true
                panel.show()
            case "disarmed":
                isArmed = false
                panel.hide()
                // A device changed while the key was held. Now that the
                // sentence is finished, follow it.
                if restartPending { scheduleCaptureRestart() }
            default:
                break
            }
        case .transcript(let text):
            lastTranscript = text
            // Straight to the focused app. We are LSUIElement/.accessory
            // and never activate, so "the focused app" is still whatever
            // the user was typing in when they pressed the key.
            inserter.insert(text)
        case .level(let value):
            peak = value
            panel.update(peak: value)
            // Log a sample periodically. A microphone that reads zero
            // from inside a bundle is the failure this spike exists to
            // catch — TCC attributing the child process to something
            // other than us — and it is otherwise indistinguishable from
            // a muted input.
            levelTicks += 1
            if Self.debugLogging && levelTicks % 12 == 0 {
                NSLog("mic peak %d", value)
            }
        case .error(let message):
            state = "error: \(message)"
            show(.error)
        case .pong(let armed):
            state = armed ? "armed" : "idle"
        case .exited(let status):
            state = "helper exited (\(status))"
            show(.stopped)
        case .unknown(let raw):
            NSLog("unrecognised event: %@", raw)
        }
        rebuildMenu()
    }

    /// Set the menu-bar icon. Falls back to a text glyph if the symbol
    /// cannot be loaded, so the item is never invisible.
    private func show(_ icon: StatusIcon) {
        guard let button = statusItem.button else { return }
        if let image = icon.image() {
            // Belt and braces with the explicit sizing in StatusIcon: if
            // an image ever does arrive oversized, shrink it rather than
            // letting the menu bar crop it.
            button.imageScaling = .scaleProportionallyDown
            button.image = image
            button.title = ""
        } else {
            button.image = nil
            button.title = icon == .armed ? "●" : "◦"
        }
        button.toolTip = icon.accessibilityDescription
    }

    // MARK: - Menu

    private func rebuildMenu() {
        let menu = NSMenu()

        menu.addItem(Self.caption(state))
        menu.addItem(Self.caption("Hold \(triggerKey.label) to talk"))
        menu.addItem(Self.caption(micDescription))

        if lastTranscript != "—" {
            menu.addItem(.separator())
            menu.addItem(Self.caption("Last: \(Self.abbreviated(lastTranscript))"))
        }

        // Raw level numbers are a diagnostic, not a feature. They earned
        // their place proving the microphone reached the child process
        // (AD-15); in normal use they just look unfinished.
        if Self.debugLogging {
            let bars = min(20, peak / 400)
            menu.addItem(
                Self.caption("mic \(String(repeating: "▁", count: max(1, bars)))  \(peak)")
            )
        }

        menu.addItem(.separator())

        let typing = NSMenuItem(
            title: "Type at cursor",
            action: #selector(toggleTyping),
            keyEquivalent: ""
        )
        typing.target = self
        typing.state = inserter.isEnabled ? .on : .off
        menu.addItem(typing)

        let keyMenu = NSMenu()
        for option in TriggerKey.allCases {
            let item = NSMenuItem(
                title: option.label,
                action: #selector(chooseTriggerKey(_:)),
                keyEquivalent: ""
            )
            item.target = self
            item.representedObject = option.rawValue
            item.state = option == triggerKey ? .on : .off
            keyMenu.addItem(item)
        }
        let keyItem = NSMenuItem(title: "Trigger key", action: nil, keyEquivalent: "")
        keyItem.submenu = keyMenu
        menu.addItem(keyItem)

        menu.addItem(.separator())
        menu.addItem(
            withTitle: "Quit Raneen",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
        statusItem.menu = menu
    }

    /// Who owns the microphone, and at what rate.
    ///
    /// The rate is here because it is the one *observable* proof that a
    /// device change was followed: the built-in microphone runs at
    /// 48 kHz and AirPods at 24 kHz, so plugging them in should visibly
    /// change this line. Without it, "followed the device" and "carried
    /// on with the old one" look identical from the outside.
    private var micDescription: String {
        guard Self.nativeAudio else { return "Mic: helper (PortAudio)" }
        guard let format = capture?.inputFormat else { return "Mic: unavailable" }
        return String(format: "Mic: this app · %.0f kHz", format.sampleRate / 1000)
    }

    /// A non-interactive line. `action: nil` already makes an item
    /// unclickable, but AppKit still draws it in full black as though it
    /// were enabled; explicitly disabling it renders it grey, which is
    /// what "this is information, not a command" looks like on macOS.
    private static func caption(_ text: String) -> NSMenuItem {
        let item = NSMenuItem(title: text, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    /// Keep the menu a sane width regardless of how much was dictated.
    private static func abbreviated(_ text: String, limit: Int = 50) -> String {
        let flat = text.replacingOccurrences(of: "\n", with: " ")
        return flat.count <= limit ? flat : flat.prefix(limit - 1) + "…"
    }

    @objc private func chooseTriggerKey(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String,
              let choice = TriggerKey(rawValue: raw),
              choice != triggerKey else { return }

        if !hotkey.rebind(to: choice) {
            NSLog("could not rebind hotkey — Accessibility not granted")
            state = "no Accessibility permission"
        } else {
            NSLog("trigger key is now %@", choice.label)
        }
        rebuildMenu()
    }

    @objc private func toggleTyping() {
        inserter.isEnabled.toggle()
        NSLog("type at cursor: %@", inserter.isEnabled ? "on" : "off")
        rebuildMenu()
    }

    private func promptForAccessibility() {
        // There is no usage-description key for Accessibility and no way
        // to request it programmatically — this only opens the pane with
        // the app pre-listed. The user still has to flip the switch.
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
    }
}
