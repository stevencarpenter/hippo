import SwiftUI

struct ContentView: View {
    @AppStorage("selectedTab") private var selectedTabRawValue = SidebarSection.query.rawValue

    private var selection: Binding<SidebarSection?> {
        Binding(
            get: { SidebarSection(rawValue: selectedTabRawValue) ?? .query },
            set: { selectedTabRawValue = ($0 ?? .query).rawValue }
        )
    }

    var body: some View {
        NavigationSplitView {
            List(SidebarSection.allCases, selection: selection) { section in
                Label(section.title, systemImage: section.systemImage)
                    .tag(Optional(section))
            }
            .navigationTitle("Hippo")
        } detail: {
            switch SidebarSection(rawValue: selectedTabRawValue) ?? .query {
            case .query:
                QueryAskView()
            case .knowledge:
                KnowledgeView()
            case .events:
                EventBrowserView()
            case .status:
                StatusView()
            }
        }
        .navigationSplitViewStyle(.balanced)
        .frame(minWidth: 800, minHeight: 500)
    }
}

#if DEBUG
#Preview {
    ContentView()
        .brainClient(PreviewBrainClient())
}
#endif
