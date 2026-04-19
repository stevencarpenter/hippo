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
                "related_events": [["id": 12, "command": "swift test"]]
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
                    "embed_text": "Refactored the app shell"
                ]]
            ]
        )
        let lexicalData = try JSONSerialization.data(
            withJSONObject: [
                "mode": "lexical",
                "events": [["event_id": 1, "command": "swift test", "cwd": "/tmp", "timestamp": 1_713_404_800_000]],
                "nodes": [["id": 2, "uuid": "node-2", "content": "raw node", "embed_text": "node embed"]]
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
    func semanticQueryResultDecodesStringEncodedArrays() throws {
        // Tags and problems_encountered arrive as JSON-escaped strings from older brain versions
        let data = try JSONSerialization.data(
            withJSONObject: [
                "summary": "Refactored query system",
                "tags": "[\"swift\",\"async\"]",
                "key_decisions": ["Use actors"],
                "problems_encountered": "[\"Sendable conformance\"]",
                "cwd": "/Users/carpenter/projects"
            ]
        )

        let result = try JSONDecoder().decode(SemanticQueryResult.self, from: data)

        #expect(result.tags == ["swift", "async"])
        #expect(result.keyDecisions == ["Use actors"])
        #expect(result.problemsEncountered == ["Sendable conformance"])
    }

    @Test
    func semanticQueryResultHandlesMissingOptionalFields() throws {
        let data = try JSONSerialization.data(withJSONObject: ["summary": "Minimal result"])

        let result = try JSONDecoder().decode(SemanticQueryResult.self, from: data)

        #expect(result.summary == "Minimal result")
        #expect(result.tags.isEmpty)
        #expect(result.keyDecisions.isEmpty)
        #expect(result.problemsEncountered.isEmpty)
        #expect(result.score == nil)
        #expect(result.gitBranch == nil)
        #expect(result.cwd == "")
    }

    @Test
    func askSourceDecodesAllFields() throws {
        let data = try JSONSerialization.data(
            withJSONObject: [
                "id": 42,
                "summary": "Updated the GUI",
                "score": 0.87,
                "cwd": "/Users/carpenter/projects/hippo",
                "git_branch": "main",
                "timestamp": 1_713_404_800_000,
                "commands_raw": "swift build",
                "uuid": "node-abc",
                "linked_event_ids": [1, 2, 3]
            ]
        )

        let source = try JSONDecoder().decode(AskSource.self, from: data)

        #expect(source.sourceId == 42)
        #expect(source.summary == "Updated the GUI")
        #expect(source.score == 0.87)
        #expect(source.cwd == "/Users/carpenter/projects/hippo")
        #expect(source.gitBranch == "main")
        #expect(source.commandsRaw == "swift build")
        #expect(source.uuid == "node-abc")
        #expect(source.linkedEventIds == [1, 2, 3])
    }

    @Test
    func askSourceDefaultsLinkedEventIdsToEmpty() throws {
        let data = try JSONSerialization.data(withJSONObject: ["summary": "A knowledge node"])

        let source = try JSONDecoder().decode(AskSource.self, from: data)

        #expect(source.linkedEventIds.isEmpty)
        #expect(source.sourceId == nil)
        #expect(source.score == nil)
        #expect(source.cwd == nil)
        #expect(source.gitBranch == nil)
    }

    @Test
    func healthResponseDecodesFromJSON() throws {
        let data = Data(
            """
            {
                "status": "degraded",
                "version": "1.2.3",
                "lmstudio_reachable": false,
                "enrichment_running": true,
                "db_reachable": true,
                "queue_depth": 5,
                "queue_failed": 1,
                "claude_queue_depth": 2,
                "claude_queue_failed": 0,
                "browser_queue_depth": 0,
                "browser_queue_failed": 0,
                "workflow_queue_depth": 3,
                "workflow_queue_failed": 2,
                "enrichment_model": "qwen",
                "enrichment_model_preferred": "qwen-large",
                "query_inflight": 1,
                "embed_model_drift": "minor",
                "last_success_at_ms": 1713404000000,
                "last_error": "timeout",
                "last_error_at_ms": 1713404100000
            }
            """.utf8
        )

        let health = try JSONDecoder().decode(HealthResponse.self, from: data)

        #expect(health.status == "degraded")
        #expect(health.version == "1.2.3")
        #expect(!health.lmstudioReachable)
        #expect(health.enrichmentRunning)
        #expect(health.dbReachable)
        #expect(health.queueDepth == 5)
        #expect(health.queueFailed == 1)
        #expect(health.claudeQueueDepth == 2)
        #expect(health.workflowQueueDepth == 3)
        #expect(health.workflowQueueFailed == 2)
        #expect(health.enrichmentModel == "qwen")
        #expect(health.enrichmentModelPreferred == "qwen-large")
        #expect(health.queryInflight == 1)
        #expect(health.embedModelDrift == "minor")
        #expect(health.lastError == "timeout")
        #expect(health.lastSuccessAtMs == 1_713_404_000_000)
        #expect(health.brainReachable)
        #expect(health.totalPendingQueueDepth == 10)
        #expect(health.totalFailedQueueDepth == 3)
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
