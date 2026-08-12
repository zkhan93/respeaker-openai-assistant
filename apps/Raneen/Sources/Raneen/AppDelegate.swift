import AppKit

/// Menu-bar shell for the spike.
///
/// Deliberately minimal: a status item, a hotkey, and a helper process.
/// The point is to prove the three things that packaging can break —
/// that the helper launches from inside a bundle, that the microphone
/// grant reaches a child process, and that an event tap can suppress a
/// key — not to be the real UI.
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {

    private var statusItem: NSStatusItem!
    private let hotkey = HotkeyTap()
    private var triggerKey: TriggerKey { hotkey.boundKey }
    private let inserter = TextInserter()
    private lazy var panel = ListeningPanel()
    private var helper: Helper?
    private var audioSocket: AudioSocket?
    private var capture: AudioCapture?
    private var earcons: EarconPlayer?

    /// Kept alive so the Core Audio listeners stay registered.
    private var deviceObservers: [Any] = []

    /// The submenus currently attached, so `menuNeedsUpdate` can tell
    /// which direction it is being asked about.
    ///
    /// **Replaced on every rebuild, never reused.** An `NSMenu` may have
    /// exactly one supermenu; attaching one instance to a second menu item
    /// throws an assertion and aborts the process. `rebuildMenu` builds a
    /// fresh `NSMenu` each time, so holding these as constants and
    /// re-attaching them crashed the app the moment anything rebuilt the
    /// menu twice.
    private var inputMenu = NSMenu()
    private var outputMenu = NSMenu()

    /// Settings, and the window that edits them. The model outlives the
    /// window so that opening it twice does not lose an unapplied edit.
    private let settings = SettingsModel()
    private lazy var settingsWindow = SettingsWindow(model: settings)

    /// Whether a turn is open, mirrored from the helper's state events.
    /// Used to avoid reopening the microphone — or replacing the core —
    /// mid-sentence.
    private var isArmed = false

    /// Reopening the microphone after a device change. Named for capture
    /// specifically because there is now a second thing that defers on
    /// `isArmed` and restarts, and the two must never be confused: this
    /// one keeps the same core running.
    private var captureRestartPending = false
    private var captureRestartAttempts = 0
    private var captureRestartWork: DispatchWorkItem?

    /// A settings change arrived while a turn was open. See
    /// `restartHelper()` — configuration is argv, so applying it means
    /// replacing the process.
    private var helperRestartPending = false

    /// A restart is between stopping the old core and spawning the new one.
    ///
    /// **Without this, two restarts in quick succession spawn two cores.**
    /// The first clears `helper` and stops the old process on a background
    /// queue; a second arriving before that finishes sees no helper to stop
    /// and spawns its own, so both completions spawn — and the loser stays
    /// alive holding a whisper model and a socket nobody reads.
    private var helperRestartInFlight = false

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
        // First thing, so anything that goes wrong during launch is
        // recorded rather than only reaching a crash report.
        Log.installCrashHandler()
        Log.app.info("Raneen starting — log at \(LogFile.shared.path)")

        // No defaults registration here any more, deliberately: `settings` is
        // a stored property and is therefore already built by this point, so
        // anything registered here would have been too late to be read. The
        // defaults live in `HelperConfig` and `SettingsStore.current` falls
        // back to them directly.
        settings.onApply = { [weak self] in self?.restartHelper() }

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        show(.starting)
        rebuildMenu()

        startHelper()

        if !hotkey.start() {
            Log.hotkey.error("event tap failed to install — Accessibility not granted")
            state = "no Accessibility permission"
            rebuildMenu()
            promptForAccessibility()
        } else {
            Log.hotkey.info("event tap installed, bound to \(self.triggerKey.label)")
            hotkey.onPress = { [weak self] in
                Log.hotkey.debug("down")
                self?.helper?.arm()
            }
            hotkey.onRelease = { [weak self] in
                Log.hotkey.debug("up")
                self?.helper?.disarm()
            }
        }
    }

    /// Opening the app while it is already running.
    ///
    /// **The menu bar is not a reliable way in.** macOS hides status items
    /// when the bar runs out of room, and an `LSUIElement` app has no Dock
    /// icon and no app menu to fall back on — so with the item hidden there
    /// was no way to reach Settings at all, short of quitting.
    ///
    /// Double-clicking Raneen in Applications is what someone tries first,
    /// and until now it did nothing at all: the second launch saw a running
    /// instance and exited without a word. `open -a Raneen` arrives here too.
    func applicationShouldHandleReopen(
        _ sender: NSApplication, hasVisibleWindows: Bool
    ) -> Bool {
        Log.app.info("reopened — showing settings")
        settingsWindow.show()
        return true
    }

    /// `open raneen://settings`, from a terminal, a Shortcut, or a launcher.
    ///
    /// The scheme is declared in Info.plist. This exists for the same reason
    /// as the reopen handler and covers the case where the app is not
    /// somewhere convenient to double-click.
    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls {
            if SettingsWindow.opensSettings(url) {
                Log.app.info("opening settings from \(url.absoluteString)")
                settingsWindow.show()
            } else {
                Log.app.error("unrecognised URL: \(url.absoluteString)")
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
        earcons?.close()
        helper?.stop()
        audioSocket?.close()
    }

    // MARK: - Helper

    private func startHelper() {
        // Checked here as well as in `spawnHelper`, so a missing core is
        // reported before we open a microphone and a socket for it.
        guard Self.locateHelper() != nil else {
            state = "helper not found"
            rebuildMenu()
            return
        }

        // Listen *before* spawning, so the helper's connect cannot race
        // us. It may sit in accept() for seconds while a Whisper model
        // loads, which is why that happens off the main queue.
        let socket = AudioSocket()
        do {
            try socket.listen()
        } catch {
            // There is deliberately no fallback to letting the helper open
            // the microphone. Two capture paths meant two behaviours for
            // device selection, hot-plug and disconnect, and only one of
            // them was ever exercised — so the other would rot silently.
            // If we cannot own the audio, we say so rather than quietly
            // running a different program than the one that was tested.
            Log.audio.error("could not create the audio socket: \(error)")
            state = "audio unavailable — \(error)"
            show(.error)
            rebuildMenu()
            return
        }
        socket.acceptInBackground()
        audioSocket = socket

        // --no-sound is only safe because we make the sound ourselves;
        // opening the device now keeps the first beep from arriving late.
        let player = EarconPlayer()
        player.deviceProvider = { DevicePreference.resolve(.output).device }
        player.prepare(device: DevicePreference.resolve(.output).device)
        earcons = player

        startCapture(sending: socket)
        watchForDeviceChanges()

        spawnHelper(sending: socket)
    }

    /// Spawn the core and wire its events up. Separate from `startHelper`
    /// because a settings change respawns it against the socket, capture
    /// and earcons that are already running.
    private func spawnHelper(sending socket: AudioSocket) {
        // Re-resolved rather than captured, so a restart picks up a changed
        // RANEEN_HELPER — which is how cores get swapped without rebuilding
        // the bundle.
        guard let executable = Self.locateHelper() else {
            state = "helper not found"
            rebuildMenu()
            return
        }

        // Read fresh, not captured: this is what makes a restart apply a
        // settings change rather than repeat the launch configuration.
        let config = SettingsStore.current()
        let arguments = config.argv(audioSocket: socket.path)

        // The whole configuration, in one line, every spawn. Worth its space
        // in the log: "why is it behaving like that" is answerable from it
        // without guessing which setting the window was showing.
        Log.helper.info("spawning core: \(arguments.joined(separator: " "))")

        let helper = Helper(executable: executable, arguments: arguments)
        helper.onEvent = { [weak self] event in
            // Events arrive on a background queue.
            DispatchQueue.main.async { self?.handle(event) }
        }
        do {
            try helper.start()
            self.helper = helper
            // Recorded only on success, and only here. The settings window
            // compares against this to decide whether Apply has anything to
            // do — so a spawn that failed must leave it alone rather than
            // claim the new configuration is running.
            settings.running = config
        } catch {
            state = "helper failed: \(error.localizedDescription)"
            rebuildMenu()
        }
    }

    /// Apply a configuration change by replacing the core.
    ///
    /// **Configuration is argv, and applying it is a restart.** The Rust
    /// core reaches `ready` in ~0.05 s, so this is imperceptible — and it
    /// keeps the core a pure function of its command line, which is what
    /// lets `protocol/` assert on behaviour from a command line alone.
    /// Reconfiguring a live core over the protocol instead would mean
    /// rebuilding the STT engine, both detectors and the ZeroMQ publisher
    /// while their threads run, in `serve.rs`'s composition root.
    ///
    /// This is only viable because the helper is Rust. The Python one took
    /// ~12 s to reach `ready`, where a restart would have been a visible
    /// outage rather than a flicker.
    ///
    /// The one setting this can never carry is a ZeroMQ auth key: argv is
    /// readable by every process on the machine through `ps`. That is what
    /// a protocol command is for, and why securing the broadcast is a
    /// separate phase rather than one more flag.
    func restartHelper() {
        guard let socket = audioSocket else { return }

        if helperRestartInFlight {
            // Dropped rather than queued: `spawnHelper` reads the current
            // configuration, so the restart already under way will come up
            // with whatever the settings say by the time it spawns.
            Log.helper.info("restart already in flight — ignoring")
            return
        }

        if isArmed {
            // The same reason capture defers: a turn is open and the user is
            // mid-sentence. Killing the core now loses those words.
            //
            // This covers every trigger, not just a held key: `isArmed` is
            // set by `listen` as well as `armed`, so a wake-word turn is
            // protected too.
            Log.helper.info("config changed while armed — restarting after the key is released")
            helperRestartPending = true
            return
        }
        helperRestartPending = false

        // Detached before stopping, so the old process's `exited` event
        // cannot reach `handle` and report a failure we asked for. Cheaper
        // and less fragile than a flag the exit path has to remember to
        // clear.
        let outgoing = helper
        outgoing?.onEvent = nil
        helper = nil
        helperRestartInFlight = true

        state = "restarting…"
        show(.starting)
        rebuildMenu()

        // `Helper.stop` waits for the process to actually go, so it must
        // not run on the main queue — a core taking its time would freeze
        // the menu bar for the whole timeout.
        DispatchQueue.global(qos: .userInitiated).async {
            outgoing?.stop()
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.helperRestartInFlight = false
                self.spawnHelper(sending: socket)
            }
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
        let (device, honoured) = DevicePreference.resolve(.input)
        do {
            try capture.start(device: device)
            self.capture = capture
            if !honoured {
                // The chosen microphone is not plugged in. Working on the
                // default beats not working, but silently listening to a
                // different device is a surprise worth surfacing.
                state = "chosen mic unavailable — using \(device?.name ?? "the default")"
            }
        } catch {
            Log.audio.error("could not start capture: \(error)")
            state = "microphone unavailable"
        }
    }

    /// Rebuild the menu, and follow the device, when the hardware changes.
    ///
    /// Core Audio notifies on both the device *list* and the system
    /// defaults. The list matters because the menu would otherwise offer
    /// devices that have gone; the defaults matter because a user who
    /// chose "System Default" expects to move with it.
    private func watchForDeviceChanges() {
        deviceObservers = AudioDevice.observe { [weak self] in
            guard let self else { return }
            self.rebuildMenu()
            // A device we explicitly wanted may have just come back, or
            // the default we were following may have moved.
            self.audioDeviceChanged()
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
    ///
    /// **Most calls here are no-ops, and that is the point.** Restarting
    /// the engine posts an `AVAudioEngineConfigurationChange`, which
    /// arrives as an interruption, which asked for another restart: the
    /// microphone was reopened several times a second, which macOS shows
    /// as its recording indicator flickering on and off. Core Audio's
    /// device-list and default-device notifications fire liberally for the
    /// same reason.
    ///
    /// So the trigger is not "something was posted", it is "the device we
    /// should be using is not the one that is open". A self-inflicted
    /// notification resolves to the device already running and stops here.
    private func audioDeviceChanged() {
        guard audioSocket != nil else { return }

        let target = DevicePreference.resolve(.input).device
        let sameDevice = capture?.openedDevice?.uid == target?.uid
        let stillRunning = capture?.isEngineRunning == true

        // Both conditions, not either. Same device but a stopped engine
        // means the change really did knock capture over and it needs
        // reopening; a running engine on the same device means this
        // notification was our own doing and there is nothing to do.
        if sameDevice && stillRunning {
            Log.devices.debug(
                "ignoring device notification — still on \(target?.name ?? "none"), engine running")
            return
        }

        Log.devices.info(
            "reopening capture: \(capture?.openedDevice?.name ?? "none") -> "
                + "\(target?.name ?? "none"), engineRunning=\(stillRunning)")

        if isArmed {
            // Reopening mid-utterance would cut the recording in half.
            // The key is still down and the old device is still feeding
            // us, so the honest move is to finish the sentence first.
            Log.devices.info("device changed while armed — deferring until the key is released")
            captureRestartPending = true
            return
        }
        scheduleCaptureRestart()
    }

    private func scheduleCaptureRestart() {
        captureRestartPending = false
        captureRestartWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.restartCapture() }
        captureRestartWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.restartDebounce, execute: work)
    }

    private func restartCapture() {
        guard let socket = audioSocket else { return }

        capture?.stop()
        capture = nil
        startCapture(sending: socket)

        if capture != nil {
            captureRestartAttempts = 0
            state = "ready"
            show(.idle)
            if let format = capture?.inputFormat {
                Log.devices.info("following the new device: \(Int(format.sampleRate)) Hz \(format.channelCount) ch")
            }
        } else {
            captureRestartAttempts += 1
            if captureRestartAttempts >= Self.maxRestartAttempts {
                state = "microphone unavailable"
                show(.error)
            } else {
                // Back off rather than hammering a device that is still
                // settling — a Bluetooth handoff takes a moment.
                let delay = Self.restartDebounce * Double(captureRestartAttempts * 2)
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                    self?.restartCapture()
                }
            }
        }
        rebuildMenu()
    }

    /// Names the bundled core may go by, in preference order.
    ///
    /// Two implementations speak the protocol (`protocol/README.md`): the
    /// Rust core and the Python one. `make dmg CORE=rust|python` decides
    /// which is inside the bundle, and this shell does not care — it
    /// spawns whichever it finds and reads the same JSON back.
    ///
    /// Rust first because it is the default: 61 MB resident against ~480,
    /// and it exits cleanly rather than orphaning.
    private static let helperNames = ["raneen-core", "voice-desktop"]

    /// Where the core lives.
    ///
    /// Inside a bundle it sits in `Contents/Resources/helper/`. During
    /// development there is no bundle, so fall back to an explicit path in
    /// the environment — that is what `make run` sets, and it is also how
    /// you swap cores against an already-built bundle without rebuilding.
    private static func locateHelper() -> URL? {
        if let override = ProcessInfo.processInfo.environment["RANEEN_HELPER"] {
            return URL(fileURLWithPath: override)
        }
        guard let resources = Bundle.main.resourceURL else { return nil }
        for name in helperNames {
            let candidate = resources.appendingPathComponent("helper/\(name)")
            if FileManager.default.isExecutableFile(atPath: candidate.path) {
                return candidate
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
            // Sound is ours when we own the audio devices — the helper is
            // run with --no-sound in that case, because its output device
            // is fixed at startup and would keep beeping into whatever was
            // connected then (AD-16).
            if let earcon = Earcon.forPattern(pattern) { earcons?.play(earcon) }
            // The panel exists to answer "is it listening?" — so it
            // tracks arming, not the per-utterance cycle.
            switch pattern {
            case "armed":
                isArmed = true
                panel.show()
            case "listen":
                // **The wake-word and always-on cue.** Only `hold` has an
                // arming layer to report; every other trigger publishes the
                // per-utterance cycle and nothing else, so without this a
                // wake word opened a turn with no indication on screen at
                // all — the core was recording and the UI looked asleep.
                //
                // `isArmed` is set here too, so a settings change or a device
                // change waits for the sentence to finish in these modes as
                // well, not only under a held key.
                isArmed = true
                panel.show()
            case "disarmed", "off":
                // `off` is the idle pattern for every non-hold trigger, so it
                // closes the turn there exactly as `disarmed` does for hold.
                isArmed = false
                panel.hide()
                // A device changed while the turn was open. Now that the
                // sentence is finished, follow it.
                if captureRestartPending { scheduleCaptureRestart() }
                // Likewise a settings change: the words are safe, so the
                // core can be replaced now.
                if helperRestartPending { restartHelper() }
            default:
                break
            }
        case .transcript(let text):
            lastTranscript = text
            // Straight to the focused app. We are LSUIElement/.accessory
            // and never activate, so "the focused app" is still whatever
            // the user was typing in when they pressed the key.
            inserter.insert(text)
        case .level(let value, let blocks):
            peak = value
            panel.update(peak: value, blocks: blocks)
            // **Deliberately returns without rebuilding the menu.**
            // Level arrives 12.5 times a second and changes nothing the
            // menu shows. Rebuilding anyway was survivable while the menu
            // was static text; once it grew device submenus it meant
            // enumerating every Core Audio device twice a frame — around
            // a thousand IPC round trips a second to coreaudiod, which is
            // enough to make the whole machine feel slow.
            levelTicks += 1
            if Self.debugLogging && levelTicks % 12 == 0 {
                Log.audio.debug("mic peak \(value)")
                rebuildMenu()  // the debug bar is the only thing that moves
            }
            return
        case .error(let message):
            state = "error: \(message)"
            show(.error)
        case .pong(let armed):
            state = armed ? "armed" : "idle"
        case .exited(let status):
            state = "helper exited (\(status))"
            show(.stopped)
        case .unknown(let raw):
            Log.helper.error("unrecognised event: \(raw)")
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

        // Fresh instances: see the note on `inputMenu`. Still empty at
        // this point — they are populated when opened (`menuNeedsUpdate`),
        // so enumerating Core Audio stays off this path, which runs often.
        inputMenu = NSMenu()
        inputMenu.delegate = self
        let inputItem = NSMenuItem(title: "Microphone", action: nil, keyEquivalent: "")
        inputItem.submenu = inputMenu
        menu.addItem(inputItem)

        outputMenu = NSMenu()
        outputMenu.delegate = self
        let outputItem = NSMenuItem(title: "Sound output", action: nil, keyEquivalent: "")
        outputItem.submenu = outputMenu
        menu.addItem(outputItem)

        menu.addItem(.separator())

        let settingsItem = NSMenuItem(
            title: "Settings…", action: #selector(openSettings), keyEquivalent: ",")
        settingsItem.target = self
        menu.addItem(settingsItem)

        // Kept alongside Settings as a diagnostic: it is the same mechanism
        // Apply uses, so being able to trigger it on its own separates "the
        // restart is broken" from "the configuration is wrong".
        let restartItem = NSMenuItem(
            title: "Restart Core", action: #selector(restartCoreFromMenu), keyEquivalent: "")
        restartItem.target = self
        menu.addItem(restartItem)

        // Somewhere to point a person who says "it broke". Without this
        // the log exists but only someone who already knows the path can
        // find it, which is the same as it not existing.
        let logItem = NSMenuItem(
            title: "Reveal Log in Finder", action: #selector(revealLog), keyEquivalent: "")
        logItem.target = self
        menu.addItem(logItem)

        menu.addItem(
            withTitle: "Quit Raneen",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
        statusItem.menu = menu
    }

    /// Which microphone is live, and at what rate.
    ///
    /// The rate earns its place: it is the one *observable* proof that a
    /// device change was actually followed — the built-in runs at 48 kHz
    /// and AirPods at 24 kHz. Without it, "followed the device" and
    /// "carried on with the old one" look identical from the outside.
    /// AppKit is about to show a submenu: this is the moment its contents
    /// need to be true, and the only moment.
    func menuNeedsUpdate(_ menu: NSMenu) {
        if menu === inputMenu {
            populate(menu, .input)
        } else if menu === outputMenu {
            populate(menu, .output)
        }
    }

    /// Read from what capture actually opened rather than re-resolving the
    /// preference. Resolving means enumerating every Core Audio device,
    /// and this line is rebuilt far more often than devices change — it is
    /// also more truthful, since it reports the device in use rather than
    /// the one we would pick if we opened now.
    private var micDescription: String {
        guard let format = capture?.inputFormat, let device = capture?.openedDevice else {
            return "Mic: unavailable"
        }
        return String(format: "Mic: %@ · %.0f kHz", device.name, format.sampleRate / 1000)
    }

    /// One direction's device list: System Default first, then everything
    /// present, with a tick against whichever is in force.
    ///
    /// Filled in on open rather than on every menu rebuild. A submenu
    /// nobody is looking at does not need to be accurate, and enumerating
    /// Core Audio is expensive enough that doing it speculatively was
    /// making the machine feel slow.
    private func populate(_ menu: NSMenu, _ direction: AudioDevice.Direction) {
        menu.removeAllItems()
        let preference = DevicePreference.current(direction)

        // Pinned at the top and separated, because it is not "one of the
        // devices" — it is the choice to keep following the system, which
        // is a different kind of answer.
        let follow = NSMenuItem(
            title: "System Default", action: #selector(chooseDevice(_:)), keyEquivalent: "")
        follow.target = self
        follow.representedObject = DeviceChoice(direction: direction, uid: nil)
        follow.state = preference == .systemDefault ? .on : .off
        menu.addItem(follow)
        menu.addItem(.separator())

        let devices = AudioDevice.all(direction)
        if devices.isEmpty {
            menu.addItem(Self.caption("No devices found"))
            return
        }

        for device in devices {
            let item = NSMenuItem(
                title: device.name, action: #selector(chooseDevice(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = DeviceChoice(direction: direction, uid: device.uid)
            item.state = preference == .explicit(uid: device.uid) ? .on : .off
            menu.addItem(item)
        }

        // An explicit choice for something that is not plugged in stays
        // ticked nowhere, so say what happened rather than showing a menu
        // with no selection at all.
        if case .explicit = preference, !devices.contains(where: {
            preference == .explicit(uid: $0.uid)
        }) {
            menu.addItem(.separator())
            menu.addItem(Self.caption("Chosen device not connected"))
        }
    }

    /// What a device menu item carries. A class because
    /// `representedObject` is `Any?` and this has to survive the round
    /// trip through AppKit.
    private final class DeviceChoice: NSObject {
        let direction: AudioDevice.Direction
        /// `nil` means System Default.
        let uid: String?

        init(direction: AudioDevice.Direction, uid: String?) {
            self.direction = direction
            self.uid = uid
        }

        var preference: DevicePreference {
            uid.map { DevicePreference.explicit(uid: $0) } ?? .systemDefault
        }
    }

    @objc private func chooseDevice(_ sender: NSMenuItem) {
        guard let choice = sender.representedObject as? DeviceChoice else { return }
        DevicePreference.set(choice.preference, for: choice.direction)

        switch choice.direction {
        case .input:
            // Through the same path a hardware change takes, so choosing a
            // microphone mid-sentence defers until the key is released
            // rather than cutting the recording in half.
            audioDeviceChanged()
        case .output:
            restartEarcons()
        }
        rebuildMenu()
    }

    private func restartEarcons() {
        earcons?.close()
        let player = EarconPlayer()
        player.deviceProvider = { DevicePreference.resolve(.output).device }
        player.prepare(device: DevicePreference.resolve(.output).device)
        earcons = player
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
            Log.hotkey.error("could not rebind — Accessibility not granted")
            state = "no Accessibility permission"
        } else {
            Log.hotkey.info("trigger key is now \(choice.label)")
        }
        rebuildMenu()
    }

    @objc private func revealLog() {
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: LogFile.shared.path)])
    }

    @objc private func restartCoreFromMenu() {
        Log.helper.info("restart requested from the menu")
        restartHelper()
    }

    @objc private func openSettings() {
        settingsWindow.show()
    }

    @objc private func toggleTyping() {
        inserter.isEnabled.toggle()
        Log.app.info("type at cursor: \(inserter.isEnabled ? "on" : "off")")
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
