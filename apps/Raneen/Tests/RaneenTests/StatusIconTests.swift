import AppKit
import XCTest

@testable import Raneen

/// Two failure modes worth pinning: a symbol name that does not resolve
/// (blank menu-bar item, indistinguishable from the app not running),
/// and an image that is taller than the menu bar (silently cropped, and
/// cropped differently on a notched display than on an external one).
final class StatusIconTests: XCTestCase {

    private let all: [StatusIcon] = [.starting, .idle, .armed, .error, .stopped]

    // MARK: - Sizing

    /// The external-monitor bug: a notched MacBook's menu bar is ~37pt
    /// while an external display is ~24pt, so anything sized to the
    /// former gets sliced in half on the latter.
    func testEveryIconFitsTheSmallestMenuBar() {
        for icon in all {
            guard let image = icon.image() else {
                XCTFail("\(icon) produced no image")
                continue
            }
            XCTAssertLessThanOrEqual(
                image.size.height, StatusIcon.menuBarHeight,
                "\(icon) is \(image.size.height)pt tall and would be clipped"
            )
        }
    }

    func testTheHeightIsSafeOnEveryDisplay() {
        // Apple's guidance for menu-bar artwork; fits a 24pt bar.
        XCTAssertLessThanOrEqual(StatusIcon.menuBarHeight, 18)
    }

    func testIconsHaveNonZeroSize() {
        for icon in all {
            let size = icon.image()?.size ?? .zero
            XCTAssertGreaterThan(size.width, 0, "\(icon) has zero width")
            XCTAssertGreaterThan(size.height, 0, "\(icon) has zero height")
        }
    }

    // MARK: - Symbols

    /// The mark asset lives in the bundle, so tests fall back to these.
    /// A misspelled name compiles fine and returns nil at runtime.
    func testEveryFallbackSymbolResolves() {
        for icon in all {
            XCTAssertNotNil(
                NSImage(systemSymbolName: icon.symbolName, accessibilityDescription: nil),
                "SF Symbol '\(icon.symbolName)' did not resolve"
            )
        }
    }

    func testEveryIconHasAnAccessibilityDescription() {
        // This is an accessibility tool; an unlabelled menu-bar button is
        // announced by VoiceOver as nothing useful.
        for icon in all {
            XCTAssertFalse(icon.accessibilityDescription.isEmpty)
            XCTAssertTrue(icon.accessibilityDescription.contains("Raneen"))
        }
    }

    // MARK: - Which states wear the brand

    func testRestingStatesUseTheMark() {
        XCTAssertTrue(StatusIcon.idle.usesMark)
        XCTAssertTrue(StatusIcon.armed.usesMark)
    }

    /// A problem should look like a problem, not like a differently
    /// shaded logo.
    func testProblemStatesUseASystemSymbolInstead() {
        XCTAssertFalse(StatusIcon.error.usesMark)
        XCTAssertFalse(StatusIcon.stopped.usesMark)
    }

    // MARK: - Pattern mapping

    func testProtocolPatternsMapToDistinctIcons() {
        XCTAssertEqual(StatusIcon.forPattern("armed"), .armed)
        XCTAssertEqual(StatusIcon.forPattern("disarmed"), .idle)
        XCTAssertEqual(StatusIcon.forPattern("error"), .error)
    }

    func testRecordingLooksDifferentFromIdle() {
        XCTAssertNotEqual(StatusIcon.armed, StatusIcon.idle)
    }

    func testUnknownPatternsFallBackRatherThanBreak() {
        XCTAssertEqual(StatusIcon.forPattern("interpretive-dance"), .idle)
        XCTAssertEqual(StatusIcon.forPattern(""), .idle)
    }

    func testEveryKnownPatternIsCovered() {
        // Mirrors voice_core.ports.indicator.KNOWN_PATTERNS. If the core
        // adds a state, this is where you notice the icon needs one too.
        for pattern in ["off", "listen", "think", "speak", "armed", "disarmed", "error"] {
            XCTAssertNotNil(StatusIcon.forPattern(pattern).image(),
                            "no usable icon for pattern '\(pattern)'")
        }
    }
}
