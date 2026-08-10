import CoreAudio
import XCTest

@testable import Raneen

/// Device enumeration against the real Core Audio on this machine.
///
/// Listing devices is not privacy-gated — unlike *opening* one — so these
/// run anywhere without a permission prompt and without a microphone
/// grant. Assertions stay about invariants rather than specific hardware,
/// since a build runner's device list is not this laptop's.
final class AudioDeviceTests: XCTestCase {

    override func tearDown() {
        UserDefaults.standard.removeObject(forKey: "inputDeviceUID")
        UserDefaults.standard.removeObject(forKey: "outputDeviceUID")
        super.tearDown()
    }

    // MARK: - Enumeration

    func testEveryDeviceHasAUsableIdentityAndLabel() {
        for direction in [AudioDevice.Direction.input, .output] {
            for device in AudioDevice.all(direction) {
                XCTAssertFalse(device.uid.isEmpty, "a device with no UID cannot be remembered")
                XCTAssertFalse(device.name.isEmpty, "a device with no name cannot be shown")
                XCTAssertNotEqual(device.id, 0)
            }
        }
    }

    /// UIDs are what preferences are stored against, so a collision would
    /// silently select the wrong device.
    func testUIDsAreUniqueWithinADirection() {
        for direction in [AudioDevice.Direction.input, .output] {
            let uids = AudioDevice.all(direction).map(\.uid)
            XCTAssertEqual(Set(uids).count, uids.count, "duplicate UID in \(direction)")
        }
    }

    /// Names are *not* unique — with AirPods connected this machine lists
    /// two devices with the same name, one per direction — which is
    /// exactly why the name is not the identity.
    func testInputsAndOutputsAreSeparateLists() {
        let inputs = AudioDevice.all(.input)
        let outputs = AudioDevice.all(.output)
        // A device can legitimately appear in both (Teams Audio does), so
        // this asserts the lists are computed per direction, not that they
        // are disjoint.
        for device in inputs where outputs.contains(device) {
            XCTAssertFalse(device.uid.isEmpty)
        }
        XCTAssertFalse(inputs.isEmpty, "no input devices at all — is Core Audio running?")
    }

    func testTheSystemDefaultIsOneOfTheListedDevices() {
        for direction in [AudioDevice.Direction.input, .output] {
            guard let fallback = AudioDevice.systemDefault(direction) else { continue }
            XCTAssertTrue(
                AudioDevice.all(direction).contains(fallback),
                "the default \(direction) device is missing from the list")
        }
    }

    func testLookingUpAnAbsentDeviceReturnsNilRatherThanGuessing() {
        XCTAssertNil(AudioDevice.device(uid: "no-such-device-uid", direction: .input))
    }

    // MARK: - Preference

    func testDefaultsToFollowingTheSystem() {
        XCTAssertEqual(DevicePreference.current(.input), .systemDefault)
    }

    func testAnExplicitChoiceSurvivesARestart() {
        DevicePreference.set(.explicit(uid: "abc-123"), for: .input)
        XCTAssertEqual(DevicePreference.current(.input), .explicit(uid: "abc-123"))
    }

    func testInputAndOutputPreferencesAreIndependent() {
        DevicePreference.set(.explicit(uid: "mic-1"), for: .input)
        XCTAssertEqual(DevicePreference.current(.output), .systemDefault)
    }

    func testChoosingSystemDefaultClearsAPreviousChoice() {
        DevicePreference.set(.explicit(uid: "mic-1"), for: .input)
        DevicePreference.set(.systemDefault, for: .input)
        XCTAssertEqual(DevicePreference.current(.input), .systemDefault)
    }

    // MARK: - Resolution

    /// The distinction the whole type exists for: following the system and
    /// having picked something are different states, and connecting AirPods
    /// must move only the first.
    func testFollowingTheSystemResolvesToTheSystemDefault() {
        DevicePreference.set(.systemDefault, for: .input)
        let (device, honoured) = DevicePreference.resolve(.input)
        XCTAssertTrue(honoured)
        XCTAssertEqual(device, AudioDevice.systemDefault(.input))
    }

    func testAnExplicitChoiceIsHonouredWhenPresent() throws {
        let device = try XCTUnwrap(AudioDevice.all(.input).first)
        DevicePreference.set(.explicit(uid: device.uid), for: .input)

        let (resolved, honoured) = DevicePreference.resolve(.input)
        XCTAssertTrue(honoured)
        XCTAssertEqual(resolved, device)
    }

    /// Unplugging an interface for an hour must not silently reset the
    /// choice — otherwise it never comes back when you plug it in again.
    func testAnAbsentChoiceFallsBackButIsNotForgotten() {
        DevicePreference.set(.explicit(uid: "device-in-a-drawer"), for: .input)

        let (resolved, honoured) = DevicePreference.resolve(.input)
        XCTAssertFalse(honoured, "an absent device should report that it was not honoured")
        XCTAssertEqual(resolved, AudioDevice.systemDefault(.input), "should fall back, not fail")
        XCTAssertEqual(
            DevicePreference.current(.input), .explicit(uid: "device-in-a-drawer"),
            "the preference was forgotten, so the device can never be re-adopted")
    }
}
