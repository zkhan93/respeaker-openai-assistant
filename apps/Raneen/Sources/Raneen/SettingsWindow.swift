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
        // The window is sized here, once, from `SettingsMetric.windowSize`
        // — not by the hosting controller, which would otherwise keep
        // re-fitting the window to its content *plus* the titlebar it lies
        // under, and grow a 640pt design into a 672pt window with a bare
        // band above the sidebar. The view is a fixed size by design, so
        // there is nothing for automatic sizing to track.
        hosting.sizingOptions = []
        let window = NSWindow(contentViewController: hosting)
        // Still set, though nothing draws it: the Window menu, ⌘` cycling
        // and VoiceOver all read this, and an untitled window is announced
        // as "window".
        window.title = "Raneen Settings"

        // **The chrome is part of the design.** `.fullSizeContentView` plus
        // a transparent titlebar is what lets the sidebar's vibrancy run to
        // the top of the window, which is the difference between a modern
        // Mac window and a document window with a toolbar. The cost is that
        // the content has to leave room for the traffic lights itself —
        // `SettingsMetric.titlebarInset` is that room, and it is why both
        // the sidebar and the pane carry a top inset rather than the usual
        // gutter.
        //
        // The title itself is hidden because the pane header already names
        // the section; showing both would say "Raneen Settings" above
        // "Dictation" and lead with the less useful of the two.
        window.styleMask = [.titled, .closable, .miniaturizable, .fullSizeContentView]
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        // No line under the (invisible) titlebar: the sidebar's vibrancy is
        // meant to run to the top edge unbroken, and the pane draws its own
        // header. Nothing here is a document with a toolbar over it.
        window.titlebarSeparatorStyle = .none
        // There is no visible titlebar to grab, so the whole surface drags
        // — as System Settings and every other titleless window does.
        window.isMovableByWindowBackground = true

        // **Sized after the style mask, on purpose.** `init(contentViewController:)`
        // measured the view at 640pt and added a titlebar's height to the
        // frame; switching to `.fullSizeContentView` afterwards then hands
        // that extra height back to the content, and a 640pt view inside a
        // 672pt content area sits at the bottom with a 32pt band of bare
        // window showing above the sidebar. It was live for one build and
        // looked like a seam between two materials.
        window.setContentSize(SettingsMetric.windowSize)

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
