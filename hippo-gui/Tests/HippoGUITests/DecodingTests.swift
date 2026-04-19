import Foundation
import Testing
@testable import HippoGUIKit

struct DecodingTests {
    @Test
    func knowledgeNodeDecodesRelatedEntitiesAndEvents() throws {
        let data = try JSONSerialization.data(
            withJSONObject: [
                "id": 1,
                "uuid": "node-1",
                "content": "{\"summary\":\"Captured a refactor\"}",
                "embed_text": "Refactored the app",
                "node_type": "observation",
                "outcome": "success",
                "tags": ["swift", "gui"],
                "created_at": 1_713_404_800_000,
                "related_entities": [["id": 9, "name": "SwiftUI", "type": "tool"]],
                "related_events": [["id": 12, "command": "swift test"]],
            ]
        )

        let node = try JSONDecoder().decode(KnowledgeNode.self, from: data)

        #expect(node.embedText == "Refactored the app")
        #expect(node.relatedEntities == [RelatedKnowledgeEntity(id: 9, name: "SwiftUI", type: "tool")])
        #expect(node.relatedEvents == [RelatedKnowledgeEvent(id: 12, command: "swift test")])
    }

    @Test
    func queryResponseDecodesSemanticAndLexicalPayloads() throws {
        let semanticData = try JSONSerialization.data(
            withJSONObject: [
                "mode": "semantic",
                "results": [[
                    "score": 0.91,
                    "summary": "Added Swift 6 view models",
                    "tags": "[\"swift\",\"mvvm\"]",
                    "key_decisions": ["Use @Observable"],
                    "problems_encountered": "[\"Module cache mismatch\"]",
                    "cwd": "/Users/carpenter/projects/hippo",
                    "git_branch": "main",
                    "session_id": 42,
                    "commands_raw": "swift build",
                    "embed_text": "Refactored the app shell",
                ]],
            ]
        )
        let lexicalData = try JSONSerialization.data(
            withJSONObject: [
                "mode": "lexical",
                "events": [["event_id": 1, "command": "swift test", "cwd": "/tmp", "timestamp": 1_713_404_800_000]],
                "nodes": [["id": 2, "uuid": "node-2", "content": "raw node", "embed_text": "node embed"]],
            ]
        )

        let semantic = try JSONDecoder().decode(QueryResponse.self, from: semanticData)
        let lexical = try JSONDecoder().decode(QueryResponse.self, from: lexicalData)

        #expect(semantic.mode == .semantic)
        #expect(semantic.results.first?.tags == ["swift", "mvvm"])
        #expect(semantic.results.first?.problemsEncountered == ["Module cache mismatch"])
        #expect(lexical.mode == .lexical)
        #expect(lexical.events.first?.eventId == 1)
        #expect(lexical.nodes.first?.embedText == "node embed")
    }

    @Test
    func paginatedResponsesDecode() throws {
        let sessions = try JSONDecoder().decode(
            SessionListResponse.self,
            from: Data(
                """
                {"sessions":[{"id":1,"start_time":1713404800000,"hostname":"laptop","shell":"zsh","event_count":2}],"total":1}
                """.utf8
            )
        )
        let events = try JSONDecoder().decode(
            EventListResponse.self,
            from: Data(
                """
                {"events":[{"id":1,"session_id":1,"timestamp":1713404800000,"command":"swift test","exit_code":0,"duration_ms":123,"cwd":"/tmp","git_branch":"main"}],"total":1}
                """.utf8
            )
        )
        let knowledge = try JSONDecoder().decode(
            KnowledgeListResponse.self,
            from: Data(
                """
                {"nodes":[{"id":1,"uuid":"node-1","content":"{}","node_type":"observation","outcome":"success","tags":["swift"],"created_at":1713404800000}],"total":1}
                """.utf8
            )
        )

        #expect(sessions.total == 1)
        #expect(events.events.first?.command == "swift test")
        #expect(knowledge.nodes.first?.nodeType == "observation")
    }
}
