import AppKit
import Carbon.HIToolbox

/// Keys that can be bound to push-to-talk.
///
/// All bare modifiers, deliberately. A modifier held on its own produces
/// no character, so binding one cannot corrupt the text you are dictating
/// into — and the *right*-hand variants are chosen because virtually
/// every system and app shortcut is typed with the left hand, so the
/// right one is usually idle.
enum TriggerKey: String, CaseIterable {
    case rightOption
    case rightCommand
    case rightControl
    case rightShift

    static let `default`: TriggerKey = .rightCommand

    var keyCode: CGKeyCode {
        switch self {
        case .rightOption:  return CGKeyCode(kVK_RightOption)
        case .rightCommand: return CGKeyCode(kVK_RightCommand)
        case .rightControl: return CGKeyCode(kVK_RightControl)
        case .rightShift:   return CGKeyCode(kVK_RightShift)
        }
    }

    var label: String {
        switch self {
        case .rightOption:  return "Right Option (⌥)"
        case .rightCommand: return "Right Command (⌘)"
        case .rightControl: return "Right Control (⌃)"
        case .rightShift:   return "Right Shift (⇧)"
        }
    }

    /// The flag this key sets, used to tell press from release —
    /// `flagsChanged` events report the resulting flag set, not a
    /// direction.
    var flag: CGEventFlags {
        switch self {
        case .rightOption:  return .maskAlternate
        case .rightCommand: return .maskCommand
        case .rightControl: return .maskControl
        case .rightShift:   return .maskShift
        }
    }

    // MARK: - Persistence

    private static let defaultsKey = "triggerKey"

    static var current: TriggerKey {
        get {
            guard let raw = UserDefaults.standard.string(forKey: defaultsKey),
                  let key = TriggerKey(rawValue: raw) else { return .default }
            return key
        }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: defaultsKey) }
    }
}
