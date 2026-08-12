import AppKit
import XCTest

@testable import Raneen

/// The bindable keys and how they persist.
final class TriggerKeyTests: XCTestCase {

    override func tearDown() {
        UserDefaults.standard.removeObject(forKey: "triggerKey")
        super.tearDown()
    }

    /// A bare modifier types nothing on its own, which is the only
    /// reason binding one is safe: the tap does not suppress it, so
    /// anything that *did* produce a character would leak into whatever
    /// is being dictated into.
    func testEveryBindableKeyIsABareModifier() {
        let modifiers: CGEventFlags = [.maskAlternate, .maskCommand, .maskControl, .maskShift]
        for key in TriggerKey.allCases {
            XCTAssertTrue(modifiers.contains(key.flag), "\(key) is not a modifier")
            XCTAssertFalse(key.flag.isEmpty, "\(key) sets no flag, so press/release is undetectable")
        }
    }

    func testEveryKeyHasADistinctKeyCode() {
        let codes = TriggerKey.allCases.map(\.keyCode)
        XCTAssertEqual(Set(codes).count, codes.count, "two keys share a key code")
    }

    func testLabelsNameTheSideOfTheKeyboard() {
        // Left-hand modifiers carry almost every system shortcut; the
        // label has to make clear which one is bound.
        for key in TriggerKey.allCases {
            XCTAssertTrue(key.label.contains("Right"), "\(key) label is ambiguous")
        }
    }

    // MARK: - Persistence

    func testDefaultsWhenNothingIsStored() {
        UserDefaults.standard.removeObject(forKey: "triggerKey")
        XCTAssertEqual(TriggerKey.current, TriggerKey.default)
    }

    func testAChoiceSurvivesARestart() {
        TriggerKey.current = .rightControl
        XCTAssertEqual(TriggerKey.current, .rightControl)
    }

    func testCorruptStoredValueFallsBackRatherThanCrashing() {
        UserDefaults.standard.set("middle-mouse-button", forKey: "triggerKey")
        XCTAssertEqual(TriggerKey.current, TriggerKey.default)
    }

    // MARK: - Arming policy

    /// The bug this whole change exists to fix: ⌘V, ⌥E and ⌃-anything
    /// all begin with the bound key going down, so arming on key-down
    /// started dictation constantly. A shortcut is a brief tap; holding
    /// the key to speak is not.
    func testTheHoldThresholdIsLongEnoughToRuleOutAShortcut() {
        XCTAssertGreaterThanOrEqual(HotkeyTap.holdThreshold, 0.15)
    }

    /// …and short enough that push-to-talk still feels immediate. The
    /// Transcriber's pre-roll reaches back past this, so nothing spoken
    /// during the window is lost.
    func testTheHoldThresholdDoesNotMakeTalkingFeelSluggish() {
        XCTAssertLessThanOrEqual(HotkeyTap.holdThreshold, 0.35)
    }

    func testTheTapBindsTheStoredKey() {
        TriggerKey.current = .rightShift
        XCTAssertEqual(HotkeyTap().boundKey, .rightShift)
    }

    func testRebindingPersistsTheNewChoice() {
        TriggerKey.current = .rightOption
        let tap = HotkeyTap()
        // Not started, so this only records the choice — no event tap and
        // therefore no Accessibility requirement.
        _ = tap.rebind(to: .rightCommand)
        XCTAssertEqual(tap.boundKey, .rightCommand)
        XCTAssertEqual(TriggerKey.current, .rightCommand)
    }
}
