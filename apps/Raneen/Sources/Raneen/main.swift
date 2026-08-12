import AppKit

// No @main: an executable target's main.swift *is* the entry point, and
// mixing the two is a compile error.
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate

// .accessory = menu-bar only, no Dock icon, and — critically for
// dictation — activating the app does not steal focus from whatever the
// user is typing into. Info.plist sets LSUIElement to match, so this
// holds from the moment of launch rather than after the first run loop
// turn. Both are needed: the plist for launch, this for a bare binary
// with no bundle around it.
app.setActivationPolicy(.accessory)
app.run()
