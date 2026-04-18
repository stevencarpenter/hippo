import SwiftUI

struct ContentView: View {
    private let brainClient = BrainClient()

    var body: some View {
        TabView {
            QueryAskView(brainClient: brainClient)
                .tabItem {
                    Label("Query", systemImage: "questionmark.circle")
                }

            KnowledgeView(brainClient: brainClient)
                .tabItem {
                    Label("Knowledge", systemImage: "brain")
                }

            EventBrowserView(brainClient: brainClient)
                .tabItem {
                    Label("Events", systemImage: "terminal")
                }

            StatusView(brainClient: brainClient)
                .tabItem {
                    Label("Status", systemImage: "heart")
                }
        }
    }
}