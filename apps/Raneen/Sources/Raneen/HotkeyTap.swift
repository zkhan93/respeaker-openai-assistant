import AppKit
import Carbon.HIToolbox

/// Global push-to-talk key, via a `CGEventTap`.
///
/// ## Why this is more than "key down, key up"
///
/// The first version armed the moment the bound modifier went down and
/// suppressed the key outright. Both were wrong, and together they made
/// dictation start when you had no intention of dictating:
///
/// * **A modifier is normally pressed as part of something else.** ⌥E,
///   ⌘⇧4, ⌃-anything — each of those begins with the bound key going
///   down, so each of them armed the microphone.
/// * **Suppressing it broke its real job.** A swallowed Right Option
///   cannot type ø or ¬, and a swallowed Right Command cannot ⌘V.
///
/// So the key is no longer suppressed — a bare modifier emits no
/// character anyway, which is the whole reason only modifiers are
/// bindable — and arming waits for evidence that you mean it:
///
/// 1. The key must be held for `holdThreshold` before anything happens.
///    Using a modifier in a shortcut is a brief tap; holding it to talk
///    is not.
/// 2. Nothing else may be pressed during that window. The instant
///    another key arrives, the pending arm is abandoned — you were
///    typing a shortcut.
///
/// The cost is `holdThreshold` of latency before recording starts, which
/// the Transcriber's pre-roll already reaches back past.
///
/// Requires Accessibility. `CGEvent.tapCreate` returns nil without it,
/// which is the only reliable signal — `AXIsProcessTrusted()` can lag
/// behind the real grant right after the user flips the switch.
final class HotkeyTap {

    /// How long the key must be held alone before dictation arms.
    /// Long enough to rule out a shortcut, short enough that holding it
    /// to speak feels immediate.
    static let holdThreshold: TimeInterval = 0.25

    private var key: TriggerKey
    private var tap: CFMachPort?
    private var source: CFRunLoopSource?

    private var isDown = false
    private var isArmed = false
    /// Bumped on every press and release so a pending arm from an
    /// earlier press can be recognised as stale and dropped.
    private var pressGeneration = 0

    /// Called on the main actor when dictation should start / stop.
    var onPress: (() -> Void)?
    var onRelease: (() -> Void)?

    init(key: TriggerKey = TriggerKey.current) {
        self.key = key
    }

    var boundKey: TriggerKey { key }

    /// Install the tap. Returns false if Accessibility has not been granted.
    @discardableResult
    func start() -> Bool {
        let mask = (1 << CGEventType.keyDown.rawValue)
            | (1 << CGEventType.keyUp.rawValue)
            | (1 << CGEventType.flagsChanged.rawValue)

        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            // .defaultTap rather than .listenOnly so the tap can be
            // re-enabled after a timeout; nothing is suppressed.
            options: .defaultTap,
            eventsOfInterest: CGEventMask(mask),
            callback: { _, type, event, context in
                let tap = Unmanaged<HotkeyTap>.fromOpaque(context!).takeUnretainedValue()
                return tap.handle(type: type, event: event)
            },
            userInfo: Unmanaged.passUnretained(self).toOpaque()
        ) else {
            return false
        }

        let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)

        self.tap = tap
        self.source = source
        return true
    }

    func stop() {
        if let tap {
            CGEvent.tapEnable(tap: tap, enable: false)
            CFMachPortInvalidate(tap)
        }
        if let source {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes)
        }
        tap = nil
        source = nil
        cancelPending()
    }

    /// Rebind to a different key, restarting the tap.
    @discardableResult
    func rebind(to newKey: TriggerKey) -> Bool {
        let wasRunning = tap != nil
        if isArmed {
            // Never leave the helper holding the microphone because the
            // key it was waiting on stopped existing.
            isArmed = false
            DispatchQueue.main.async { [weak self] in self?.onRelease?() }
        }
        stop()
        key = newKey
        TriggerKey.current = newKey
        return wasRunning ? start() : true
    }

    // MARK: - Callback

    private func handle(type: CGEventType, event: CGEvent) -> Unmanaged<CGEvent>? {
        // The system disables a tap that takes too long, or after the
        // display sleeps. Re-enabling is the documented recovery;
        // without it the hotkey silently stops working until restart.
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            return Unmanaged.passUnretained(event)
        }

        let code = CGKeyCode(event.getIntegerValueField(.keyboardEventKeycode))

        if code == key.keyCode && type == .flagsChanged {
            let down = event.flags.contains(key.flag)
            if down != isDown {
                isDown = down
                down ? beginPending() : endHold()
            }
        } else if type == .keyDown || (type == .flagsChanged && code != key.keyCode) {
            // Something else was pressed. If our key is down, it is being
            // used as a modifier — that is a shortcut, not dictation.
            if isDown && !isArmed {
                cancelPending()
            }
        }

        // Always pass through. See the class note: suppressing a bare
        // modifier breaks the thing it exists to do.
        return Unmanaged.passUnretained(event)
    }

    private func beginPending() {
        pressGeneration &+= 1
        let generation = pressGeneration
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.holdThreshold) { [weak self] in
            guard let self,
                  self.pressGeneration == generation,
                  self.isDown,
                  !self.isArmed else { return }
            self.isArmed = true
            self.onPress?()
        }
    }

    private func endHold() {
        cancelPending()
        guard isArmed else { return }
        isArmed = false
        DispatchQueue.main.async { [weak self] in self?.onRelease?() }
    }

    /// Invalidate any pending arm without touching an active one.
    private func cancelPending() {
        pressGeneration &+= 1
    }
}
