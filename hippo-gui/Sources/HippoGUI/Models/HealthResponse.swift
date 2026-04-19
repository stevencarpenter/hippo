import Foundation

struct HealthResponse: Codable, Hashable, Sendable {
    let status: String
    let version: String?
    let lmstudioReachable: Bool
    let enrichmentRunning: Bool
    let dbReachable: Bool
    let queueDepth: Int
    let queueFailed: Int
    let claudeQueueDepth: Int
    let claudeQueueFailed: Int
    let browserQueueDepth: Int
    let browserQueueFailed: Int
    let workflowQueueDepth: Int
    let workflowQueueFailed: Int
    let enrichmentModel: String?
    let enrichmentModelPreferred: String?
    let queryInflight: Int?
    let embedModelDrift: String?
    let lastSuccessAtMs: Int?
    let lastError: String?
    let lastErrorAtMs: Int?

    enum CodingKeys: String, CodingKey {
        case status
        case version
        case lmstudioReachable = "lmstudio_reachable"
        case enrichmentRunning = "enrichment_running"
        case dbReachable = "db_reachable"
        case queueDepth = "queue_depth"
        case queueFailed = "queue_failed"
        case claudeQueueDepth = "claude_queue_depth"
        case claudeQueueFailed = "claude_queue_failed"
        case browserQueueDepth = "browser_queue_depth"
        case browserQueueFailed = "browser_queue_failed"
        case workflowQueueDepth = "workflow_queue_depth"
        case workflowQueueFailed = "workflow_queue_failed"
        case enrichmentModel = "enrichment_model"
        case enrichmentModelPreferred = "enrichment_model_preferred"
        case queryInflight = "query_inflight"
        case embedModelDrift = "embed_model_drift"
        case lastSuccessAtMs = "last_success_at_ms"
        case lastError = "last_error"
        case lastErrorAtMs = "last_error_at_ms"
    }

    var brainReachable: Bool {
        status == "ok" || status == "degraded"
    }

    var totalPendingQueueDepth: Int {
        queueDepth + claudeQueueDepth + browserQueueDepth + workflowQueueDepth
    }

    var totalFailedQueueDepth: Int {
        queueFailed + claudeQueueFailed + browserQueueFailed + workflowQueueFailed
    }
}

#if DEBUG
extension HealthResponse {
    static let preview = HealthResponse(
        status: "ok",
        version: "preview",
        lmstudioReachable: true,
        enrichmentRunning: true,
        dbReachable: true,
        queueDepth: 3,
        queueFailed: 1,
        claudeQueueDepth: 2,
        claudeQueueFailed: 0,
        browserQueueDepth: 1,
        browserQueueFailed: 0,
        workflowQueueDepth: 0,
        workflowQueueFailed: 0,
        enrichmentModel: "qwen-preview",
        enrichmentModelPreferred: "qwen-preview",
        queryInflight: nil,
        embedModelDrift: nil,
        lastSuccessAtMs: 1_713_404_800_000,
        lastError: nil,
        lastErrorAtMs: nil
    )
}
#endif
