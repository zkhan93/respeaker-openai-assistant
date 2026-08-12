import Foundation
import XCTest

@testable import Raneen

/// Reaching Settings without the menu bar.
///
/// macOS hides status items when the menu bar runs out of room, and an
/// `LSUIElement` app has no Dock icon or app menu to fall back on — so these
/// routes are the only way in when that happens, not a convenience.
final class SettingsURLTests: XCTestCase {

    func testTheDocumentedFormOpensSettings() {
        XCTAssertTrue(SettingsWindow.opensSettings(URL(string: "raneen://settings")!))
    }

    /// Typed without slashes, which is just as natural and parses completely
    /// differently: the whole thing becomes opaque and `host` is nil.
    func testTheSchemeOnlyFormOpensSettings() {
        XCTAssertTrue(SettingsWindow.opensSettings(URL(string: "raneen:settings")!))
    }

    /// There is nothing else a bare `raneen://` could reasonably mean.
    func testABareSchemeOpensSettings() {
        XCTAssertTrue(SettingsWindow.opensSettings(URL(string: "raneen://")!))
    }

    /// URL schemes are case-insensitive, and anything that opens one on the
    /// user's behalf may normalise it.
    func testCaseIsIgnored() {
        XCTAssertTrue(SettingsWindow.opensSettings(URL(string: "RANEEN://Settings")!))
    }

    func testTrailingSlashesDoNotMatter() {
        XCTAssertTrue(SettingsWindow.opensSettings(URL(string: "raneen://settings/")!))
    }

    /// Another app's scheme must not be answered. Nothing stops a second app
    /// registering a URL type, and the handler is reached for whatever macOS
    /// decides to hand over.
    func testAnotherSchemeIsRefused() {
        XCTAssertFalse(SettingsWindow.opensSettings(URL(string: "https://example.com/settings")!))
        XCTAssertFalse(SettingsWindow.opensSettings(URL(string: "file:///tmp/settings")!))
    }

    /// An unknown action is refused rather than treated as "settings", so
    /// adding `raneen://record` later cannot silently open the wrong thing in
    /// a build that predates it.
    func testAnUnknownActionIsRefused() {
        XCTAssertFalse(SettingsWindow.opensSettings(URL(string: "raneen://record")!))
        XCTAssertFalse(SettingsWindow.opensSettings(URL(string: "raneen://quit")!))
    }
}
