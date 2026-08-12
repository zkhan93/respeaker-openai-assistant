import CoreAudio
import Foundation

/// The machine's audio devices, and which one we have been told to use.
///
/// This is the capability that justified moving capture out of Python at
/// all (ROADMAP AD-16). PortAudio caches its device list at init, renumbers
/// indices whenever anything connects, and exposes no stable identifier —
/// so "use this microphone" could not be expressed in a way that survived
/// unplugging a pair of headphones. Core Audio gives every device a
/// persistent UID, and that is what a preference is stored against.
struct AudioDevice: Equatable {
    let id: AudioDeviceID
    /// Stable across reboots and reconnects. The *only* safe thing to
    /// persist — names collide (two identical interfaces, or two people's
    /// AirPods) and IDs are reassigned freely.
    let uid: String
    let name: String

    enum Direction {
        case input, output

        var scope: AudioObjectPropertyScope {
            self == .input ? kAudioDevicePropertyScopeInput : kAudioDevicePropertyScopeOutput
        }

        var defaultSelector: AudioObjectPropertySelector {
            self == .input
                ? kAudioHardwarePropertyDefaultInputDevice
                : kAudioHardwarePropertyDefaultOutputDevice
        }
    }

    // MARK: - Enumeration

    /// Every device that can carry audio in `direction`.
    ///
    /// Filtered by channel count rather than by name or type: an aggregate
    /// device, a virtual one like Teams or Loopback, and a USB interface
    /// are all legitimate here, and the only thing that actually matters
    /// is whether it has channels pointing the right way.
    static func all(_ direction: Direction) -> [AudioDevice] {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var size: UInt32 = 0
        guard
            AudioObjectGetPropertyDataSize(
                AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size) == noErr,
            size > 0
        else { return [] }

        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard
            AudioObjectGetPropertyData(
                AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &ids) == noErr
        else { return [] }

        return ids.compactMap { id in
            guard channelCount(id, direction) > 0,
                !isPrivate(id),
                let uid = string(id, kAudioDevicePropertyDeviceUID),
                let name = string(id, kAudioObjectPropertyName)
            else { return nil }
            return AudioDevice(id: id, uid: uid, name: name)
        }
    }

    /// Whether Core Audio considers this device an implementation detail.
    ///
    /// `CADefaultDeviceAggregate` is the one that prompted this: the HAL
    /// builds a private aggregate to stand for "the default device" when
    /// an app opens one, so **we were listing a device our own
    /// `AVAudioEngine` had just caused to exist**. It is a real HAL object
    /// and completely useless as a choice.
    ///
    /// Two independent signals, because neither covers everything:
    ///
    /// * `kAudioDevicePropertyIsHidden` — the general "do not show this"
    ///   flag. Hidden devices still appear in the device list; the flag is
    ///   what says not to offer them.
    /// * a private *aggregate*, via the `IsPrivate` key in its
    ///   composition. Deliberately narrow: user-created aggregates and
    ///   Multi-Output Devices are legitimate choices and must stay.
    private static func isPrivate(_ id: AudioDeviceID) -> Bool {
        if let hidden = uint32(id, kAudioDevicePropertyIsHidden), hidden != 0 { return true }

        var address = AudioObjectPropertyAddress(
            mSelector: kAudioAggregateDevicePropertyComposition,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var composition: Unmanaged<CFDictionary>?
        var size = UInt32(MemoryLayout<Unmanaged<CFDictionary>?>.size)
        // Fails for anything that is not an aggregate, which is the
        // common case and not an error.
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, &composition) == noErr,
            let dictionary = composition?.takeRetainedValue() as? [String: Any]
        else { return false }

        let key = kAudioAggregateDeviceIsPrivateKey as String
        if let flag = dictionary[key] as? Int { return flag != 0 }
        if let flag = dictionary[key] as? Bool { return flag }
        return false
    }

    private static func uint32(
        _ id: AudioDeviceID, _ selector: AudioObjectPropertySelector
    ) -> UInt32? {
        var address = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, &value) == noErr else {
            return nil
        }
        return value
    }

    /// What the system would pick right now.
    static func systemDefault(_ direction: Direction) -> AudioDevice? {
        var address = AudioObjectPropertyAddress(
            mSelector: direction.defaultSelector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var id = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard
            AudioObjectGetPropertyData(
                AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &id) == noErr,
            id != 0
        else { return nil }
        return all(direction).first { $0.id == id }
    }

    /// Resolve a stored preference. Nil when that device is not present —
    /// headphones in a drawer — which the caller treats as "fall back to
    /// the system default for now, and re-adopt this when it returns".
    static func device(uid: String, direction: Direction) -> AudioDevice? {
        all(direction).first { $0.uid == uid }
    }

    // MARK: - Change notification

    /// Call `onChange` whenever the device list or a system default moves.
    ///
    /// Both matter and for different reasons: the list changing means the
    /// menu is stale, while a default moving means the device we are
    /// *using* may have changed under us. Returns a token to keep alive;
    /// releasing it stops the notifications.
    static func observe(_ onChange: @escaping () -> Void) -> [Any] {
        let selectors = [
            kAudioHardwarePropertyDevices,
            kAudioHardwarePropertyDefaultInputDevice,
            kAudioHardwarePropertyDefaultOutputDevice,
        ]
        let queue = DispatchQueue.main
        return selectors.compactMap { selector -> Any? in
            var address = AudioObjectPropertyAddress(
                mSelector: selector,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain
            )
            let block: AudioObjectPropertyListenerBlock = { _, _ in onChange() }
            let status = AudioObjectAddPropertyListenerBlock(
                AudioObjectID(kAudioObjectSystemObject), &address, queue, block)
            return status == noErr ? block : nil
        }
    }

    // MARK: - Core Audio boilerplate

    private static func channelCount(_ id: AudioDeviceID, _ direction: Direction) -> Int {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: direction.scope,
            mElement: kAudioObjectPropertyElementMain
        )
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(id, &address, 0, nil, &size) == noErr, size > 0 else {
            return 0
        }
        let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: 16)
        defer { raw.deallocate() }
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, raw) == noErr else { return 0 }

        let list = UnsafeMutableAudioBufferListPointer(
            raw.assumingMemoryBound(to: AudioBufferList.self))
        return list.reduce(0) { $0 + Int($1.mNumberChannels) }
    }

    private static func string(
        _ id: AudioDeviceID, _ selector: AudioObjectPropertySelector
    ) -> String? {
        var address = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        // Unmanaged, because these properties hand back a +1 CFString the
        // caller owns. Reading into a plain `CFString` compiles but leaks
        // one string per device per enumeration — and the menu
        // re-enumerates on every device change.
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, &value) == noErr,
            let string = value?.takeRetainedValue() as String?
        else { return nil }
        return string.isEmpty ? nil : string
    }
}

/// Which device the user asked for, per direction.
///
/// **"System Default" is a first-class choice, not the absence of one.**
/// Conflating the two is the bug this type exists to prevent: if you pick
/// *System Default* and then connect AirPods, we should follow macOS; if
/// you pick *MacBook Pro Microphone* explicitly, connecting AirPods must
/// not silently move you. Storing nil for both makes those indistinguishable.
enum DevicePreference: Equatable {
    case systemDefault
    case explicit(uid: String)

    private static func key(_ direction: AudioDevice.Direction) -> String {
        direction == .input ? "inputDeviceUID" : "outputDeviceUID"
    }

    static func current(_ direction: AudioDevice.Direction) -> DevicePreference {
        guard let uid = UserDefaults.standard.string(forKey: key(direction)), !uid.isEmpty else {
            return .systemDefault
        }
        return .explicit(uid: uid)
    }

    static func set(_ preference: DevicePreference, for direction: AudioDevice.Direction) {
        switch preference {
        case .systemDefault:
            UserDefaults.standard.removeObject(forKey: key(direction))
        case .explicit(let uid):
            UserDefaults.standard.set(uid, forKey: key(direction))
        }
    }

    /// The device to actually open, and whether it is what was asked for.
    ///
    /// A preferred device that is absent resolves to the system default
    /// *without* forgetting the preference — unplugging an interface for
    /// an hour should not silently reset your choice, and re-adoption when
    /// it returns is what makes this feel like every other app.
    static func resolve(_ direction: AudioDevice.Direction) -> (device: AudioDevice?, honoured: Bool)
    {
        switch current(direction) {
        case .systemDefault:
            return (AudioDevice.systemDefault(direction), true)
        case .explicit(let uid):
            if let device = AudioDevice.device(uid: uid, direction: direction) {
                return (device, true)
            }
            return (AudioDevice.systemDefault(direction), false)
        }
    }
}
