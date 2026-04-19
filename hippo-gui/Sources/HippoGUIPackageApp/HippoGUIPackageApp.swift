import SwiftUI
import HippoGUIKit

@main
struct HippoGUIPackageApp: App {
    @NSApplicationDelegateAdaptor(HippoGUIAppDelegate.self) private var appDelegate

    var body: some Scene {
        HippoGUIMainScene()
    }
}
