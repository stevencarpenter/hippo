import SwiftUI

struct ContentView: View {
    @State private var brainClient: BrainClient?

    var body: some View {
        Group {
            if let client = brainClient {
                TabView {
                    QueryAskView(brainClient: client)
                        .tabItem {
                            Label("Query", systemImage: "questionmark.circle")
                        }

                    KnowledgeView(brainClient: client)
                        .tabItem {
                            Label("Knowledge", systemImage: "brain")
                        }

                    EventBrowserView(brainClient: client)
                        .tabItem {
                            Label("Events", systemImage: "terminal")
                        }

                    StatusView(brainClient: client)
                        .tabItem {
                            Label("Status", systemImage: "heart")
                        }
                }
            } else {
                ProgressView("Connecting...")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .task {
            brainClient = await BrainClient.makeDefault()
        }
    }
}
