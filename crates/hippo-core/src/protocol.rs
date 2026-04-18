use serde::{Deserialize, Serialize};

use crate::events::EventEnvelope;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", content = "data")]
pub enum DaemonRequest {
    IngestEvent(Box<EventEnvelope>),
    GetStatus,
    GetSessions {
        since_ms: Option<i64>,
        limit: Option<usize>,
    },
    GetEvents {
        session_id: Option<i64>,
        since_ms: Option<i64>,
        project: Option<String>,
        limit: Option<usize>,
    },
    GetEntities {
        entity_type: Option<String>,
    },
    RawQuery {
        text: String,
    },
    RegisterWatchSha {
        sha: String,
        repo: String,
        ttl_secs: u64,
    },
    Shutdown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", content = "data")]
pub enum DaemonResponse {
    Ack,
    Status(StatusInfo),
    Sessions(Vec<SessionInfo>),
    Events(Vec<EventInfo>),
    Entities(Vec<EntityInfo>),
    QueryResult(Vec<QueryHit>),
    Error(String),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StatusInfo {
    #[serde(default)]
    pub version: String,
    pub uptime_secs: u64,
    pub events_today: u64,
    pub sessions_today: u64,
    pub queue_depth: u64,
    pub queue_failed: u64,
    pub drop_count: u64,
    pub lmstudio_reachable: bool,
    pub brain_reachable: bool,
    pub db_size_bytes: u64,
    pub fallback_files_pending: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionInfo {
    pub id: i64,
    pub start_time: i64,
    pub end_time: Option<i64>,
    pub hostname: String,
    pub shell: String,
    pub event_count: u64,
    pub summary: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EventInfo {
    pub id: i64,
    pub session_id: i64,
    pub timestamp: i64,
    pub command: String,
    pub exit_code: Option<i32>,
    pub duration_ms: u64,
    pub cwd: String,
    pub git_branch: Option<String>,
    pub enriched: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EntityInfo {
    pub id: i64,
    pub entity_type: String,
    pub name: String,
    pub canonical: Option<String>,
    pub first_seen: i64,
    pub last_seen: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueryHit {
    pub event_id: i64,
    pub command: String,
    pub cwd: String,
    pub timestamp: i64,
    pub relevance: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_request_roundtrip() {
        let requests = vec![
            DaemonRequest::GetStatus,
            DaemonRequest::GetSessions {
                since_ms: Some(1000),
                limit: Some(10),
            },
            DaemonRequest::GetEvents {
                session_id: None,
                since_ms: Some(5000),
                project: Some("hippo".to_string()),
                limit: None,
            },
            DaemonRequest::GetEntities {
                entity_type: Some("tool".to_string()),
            },
            DaemonRequest::RawQuery {
                text: "cargo build".to_string(),
            },
            DaemonRequest::Shutdown,
        ];
        for req in requests {
            let json = serde_json::to_string(&req).unwrap();
            let parsed: DaemonRequest = serde_json::from_str(&json).unwrap();
            let json2 = serde_json::to_string(&parsed).unwrap();
            assert_eq!(json, json2);
        }
    }

    #[test]
    fn test_response_roundtrip() {
        let responses = vec![
            DaemonResponse::Ack,
            DaemonResponse::Status(StatusInfo {
                version: "0.5.0-dev.3+gfaa08aa".to_string(),
                uptime_secs: 3600,
                events_today: 42,
                sessions_today: 3,
                queue_depth: 5,
                queue_failed: 0,
                drop_count: 0,
                lmstudio_reachable: true,
                brain_reachable: true,
                db_size_bytes: 1024000,
                fallback_files_pending: 0,
            }),
            DaemonResponse::Sessions(vec![SessionInfo {
                id: 1,
                start_time: 1000000,
                end_time: None,
                hostname: "laptop".to_string(),
                shell: "zsh".to_string(),
                event_count: 10,
                summary: Some("coding session".to_string()),
            }]),
            DaemonResponse::Events(vec![EventInfo {
                id: 1,
                session_id: 1,
                timestamp: 1000000,
                command: "cargo test".to_string(),
                exit_code: Some(0),
                duration_ms: 500,
                cwd: "/home/user".to_string(),
                git_branch: Some("main".to_string()),
                enriched: false,
            }]),
            DaemonResponse::Entities(vec![EntityInfo {
                id: 1,
                entity_type: "tool".to_string(),
                name: "cargo".to_string(),
                canonical: Some("cargo".to_string()),
                first_seen: 1000,
                last_seen: 2000,
            }]),
            DaemonResponse::QueryResult(vec![QueryHit {
                event_id: 1,
                command: "cargo build".to_string(),
                cwd: "/project".to_string(),
                timestamp: 1000,
                relevance: "exact".to_string(),
            }]),
            DaemonResponse::Error("something went wrong".to_string()),
        ];
        for resp in responses {
            let json = serde_json::to_string(&resp).unwrap();
            let parsed: DaemonResponse = serde_json::from_str(&json).unwrap();
            let json2 = serde_json::to_string(&parsed).unwrap();
            assert_eq!(json, json2);
        }
    }

    #[test]
    fn test_status_info_deserializes_without_version_field() {
        let json = r#"{"type":"Status","data":{"uptime_secs":100,"events_today":5,"sessions_today":1,"queue_depth":0,"queue_failed":0,"drop_count":0,"lmstudio_reachable":false,"brain_reachable":false,"db_size_bytes":0,"fallback_files_pending":0}}"#;
        let resp: DaemonResponse = serde_json::from_str(json).unwrap();
        match resp {
            DaemonResponse::Status(status) => assert!(status.version.is_empty()),
            other => panic!("expected Status, got {:?}", other),
        }
    }
}
