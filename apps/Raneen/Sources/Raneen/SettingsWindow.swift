import AppKit
import SwiftUI

/// The window the settings view lives in.
///
/// Hand-built rather than a SwiftUI `Settings` scene: this app has an
/// `NSApplicationDelegate` and no `App` struct, because the menu bar, the
/// event tap and the non-activating listening panel all need AppKit. So the
/// window is AppKit and the content is hosted inside it.
///
/// **Unlike `ListeningPanel`, this window is meant to take focus.** That
/// panel goes to great lengths never to become key, because the text cursor
/// must stay where the user left it. Here the opposite is true — the user is
/// coming to change a setting, not to dictate — so opening it activates the
/// app in the ordinary way.
/// Not `@MainActor`-annotated, matching the rest of the shell: `AppDelegate`
/// is plain AppKit and everything here is reached from the main queue by
/// construction, through a menu action.
final class SettingsWindow {

    private var window: NSWindow?
    private let model: SettingsModel

    init(model: SettingsModel) {
        self.model = model
    }

    func show() {
        if let window {
            // Already built: bring it forward rather than making a second
            // one. Two settings windows editing the same defaults would
            // disagree the moment either was touched.
            NSApp.activate(ignoringOtherApps: true)
            window.makeKeyAndOrderFront(nil)
            model.refreshLibraries()
            return
        }

        let hosting = NSHostingController(rootView: SettingsView(model: model))
        let window = NSWindow(contentViewController: hosting)
        window.title = "Raneen Settings"
        window.styleMask = [.titled, .closable, .miniaturizable]
        window.isReleasedWhenClosed = false
        window.center()
        self.window = window

        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    /// Whether a URL is asking for this window.
    ///
    /// Both spellings are accepted because both are natural to type and the
    /// difference is invisible to the person typing it: `raneen://settings`
    /// puts "settings" in `host`, while `raneen:settings` — no slashes —
    /// makes the whole thing opaque and leaves `host` nil. A bare
    /// `raneen://` opens Settings too, since there is nothing else it could
    /// reasonably mean.
    static func opensSettings(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == "raneen" else { return false }
        let target =
            url.host
            ?? url.absoluteString
            .dropFirst("raneen:".count)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        return target.isEmpty || target.lowercased() == "settings"
    }
}
