import SwiftUI
import HippoGUIKit

@main
struct HippoGUIXcodeApp: App {
    @NSApplicationDelegateAdaptor(HippoGUIAppDelegate.self) private var appDelegate

    var body: some Scene {
        HippoGUIMainScene()
    }
}
