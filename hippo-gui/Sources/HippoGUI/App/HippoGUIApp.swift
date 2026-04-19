import AppKit
import SwiftUI

private struct HippoGUIAppCommands: Commands {
    @Environment(\.openWindow) private var openWindow

    private var aboutWindowTitle: String { "About HippoGUI" }

    private func focusExistingAboutWindow() -> Bool {
        guard let window = NSApp.windows.first(where: { $0.title == aboutWindowTitle }) else {
            return false
        }

        NSApp.activate(ignoringOtherApps: true)
        if window.isMiniaturized {
            window.deminiaturize(nil)
        }
        window.makeKeyAndOrderFront(nil)
        return true
    }

    var body: some Commands {
        CommandGroup(replacing: .appInfo) {
            Button("About HippoGUI") {
                if !focusExistingAboutWindow() {
                    openWindow(id: "about")
                }
            }
        }
    }
}

public final class HippoGUIAppDelegate: NSObject, NSApplicationDelegate {
    public override init() {
        super.init()
    }

    public func applicationWillFinishLaunching(_ notification: Notification) {
        if Bundle.main.bundleIdentifier == nil {
            NSWindow.allowsAutomaticWindowTabbing = false
        }
    }

    public func applicationShouldSaveApplicationState(_ sender: NSApplication) -> Bool {
        Bundle.main.bundleIdentifier != nil
    }

    public func applicationShouldRestoreApplicationState(_ sender: NSApplication) -> Bool {
        Bundle.main.bundleIdentifier != nil
    }
}

public struct HippoGUIRootView: View {
    public init() {}

    public var body: some View {
        ContentView()
    }
}

public struct HippoGUIMainScene: Scene {
    @State private var brainClient = BrainClient()

    public init() {}

    public var body: some Scene {
        Window("Hippo GUI", id: "main") {
            HippoGUIRootView()
                .brainClient(brainClient)
        }
        .defaultSize(width: 900, height: 600)
        .windowResizability(.contentMinSize)
        .commands {
            HippoGUIAppCommands()
        }

        Window("About HippoGUI", id: "about") {
            AboutSettingsView()
        }
        .defaultSize(width: 460, height: 320)
        .windowResizability(.contentSize)

        Settings {
            AboutSettingsView()
        }
    }
}
