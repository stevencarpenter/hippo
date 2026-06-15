from hippo_brain.vault_render import slugify, entity_slug, node_source_key


def test_slugify_strips_obsidian_reserved_chars():
    # concept canonicals are raw error text with [ ] : / " and #
    assert slugify("error[E0382]: borrow of moved value") == "error-e0382-borrow-of-moved-value"
    assert slugify("crates/hippo-core/src/storage.rs") == "crates-hippo-core-src-storage-rs"
    assert slugify("  Multiple   Spaces  ") == "multiple-spaces"
    assert slugify("") == "unnamed"


def test_entity_slug_prefers_canonical_falls_back_to_name_then_id():
    assert entity_slug("project", "hippo", "hippo", 42) == "hippo"
    # NULL/empty canonical -> use name
    assert entity_slug("file", "/abs/path.rs", None, 7) == "abs-path-rs"
    # both empty -> use id
    assert entity_slug("concept", "", "", 9) == "entity-9"


def test_node_source_key_agentic_min_session_segment():
    # node links two agentic sessions; pick the min (session_id, segment_index)
    links = {
        "agentic": [("codex", "zzz", 5), ("claude-code", "aaa", 2), ("claude-code", "aaa", 0)],
    }
    assert node_source_key(links, node_type="observation", uuid="u1") == "claude-code-aaa-0"


def test_node_source_key_priority_and_change_outcome_discriminator():
    # workflow + agentic present -> agentic wins by priority; change_outcome gets -co suffix
    links = {"agentic": [("claude-code", "aaa", 0)], "workflow": [101]}
    assert node_source_key(links, node_type="change_outcome", uuid="u2") == "claude-code-aaa-0-co"
    # only workflow
    assert (
        node_source_key({"workflow": [101]}, node_type="change_outcome", uuid="u3") == "wf-101-co"
    )
    # no links at all -> uuid fallback
    assert node_source_key({}, node_type="observation", uuid="u4") == "node-u4"
