//! Inspection reads for ONE memory: the claims it backs, its revision
//! history, and the entities the store linked it to.
//!
//! WHY THIS EXISTS: every explorer of a store — the Memory Atlas exporter,
//! the CLI, the MCP server, a terminal UI — needs exactly these three
//! reads, and until now they existed only as SQL against `claims`,
//! `record_revisions` and `memory_entities`. Reading a live store with a
//! second SQLite library in the engine's process is forbidden
//! (CONCURRENCY.md rule 9), so an in-process explorer had no honest way to
//! show a memory's claims or its prior text. These methods are that way.
//!
//! They are READ-ONLY and leave no trace: no access-count reinforcement,
//! no demand capture, no oplog entry. Archaeology must not masquerade as
//! usage (the same rule `recall_as_of` follows).
//!
//! Encrypted stores: `record_revisions` archives `prior_text` and
//! `prior_metadata` in STORED form (ciphertext on an encrypted DB), because
//! `correct()` copies the row as it stands. `revision_history` decrypts on
//! hydration exactly as the as-of rollback does, so callers never see
//! ciphertext or a `Null` metadata produced by parsing it.

use rusqlite::params;

use crate::error::Result;

use super::YantrikDB;

/// One archived pre-correction state of a memory, as written by `correct()`.
///
/// `applied_at` is TRANSACTION time — when the store learned of the change —
/// not the time the fact changed in the world. That is what makes
/// `recall_as_of` honest, and it is why a backdated correction is not a
/// thing this engine offers.
#[derive(Debug, Clone, serde::Serialize)]
pub struct RevisionEntry {
    pub revision_id: String,
    pub rid: String,
    /// 1 for the first correction of a memory, then 2, 3, and so on.
    pub revision_num: i64,
    /// The text BEFORE this correction was applied (decrypted).
    pub prior_text: String,
    /// The metadata BEFORE this correction (decrypted, parsed; `Null` only
    /// when the archived JSON itself does not parse).
    pub prior_metadata: serde_json::Value,
    pub prior_importance: f64,
    pub prior_valence: f64,
    /// The reason the caller gave `correct()`; never empty by contract.
    pub reason: String,
    /// Unix seconds; when the correction was applied to the store.
    pub applied_at: f64,
    pub origin_actor: String,
}

fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

type RevisionRow = (
    String,
    String,
    i64,
    String,
    String,
    f64,
    f64,
    String,
    f64,
    String,
);

impl YantrikDB {
    /// Active (non-tombstoned) claims whose `source_memory_rid` is `rid`,
    /// newest first, in the same shape `get_claims` returns for an entity,
    /// including the read-time `status_suggestion` (active / superseded /
    /// historical / conflicted / negative), derived by the same rules so an
    /// explorer shows one memory's claims exactly as an entity view would.
    ///
    /// A memory that was corrected after a claim was attached keeps that
    /// claim active here (`valid_to` stays unset): `correct()` does not
    /// re-ground claims. Callers that want to flag this should compare the
    /// claim's `created_at` with `revision_history(rid)`; the Memory Atlas
    /// does exactly that.
    pub fn claims_for_memory(&self, rid: &str) -> Result<Vec<serde_json::Value>> {
        let now = now_secs();
        let conn = self.read_conn();

        // Open conflicts reference either a claim id or a memory rid on
        // each side; a claim is "conflicted" when either its own id or its
        // source memory appears. Mirrors `get_claims` so both views agree.
        let conflict_rids: std::collections::HashSet<String> = {
            let mut stmt = conn.prepare(
                "SELECT memory_a FROM conflicts WHERE status = 'open' \
                 UNION SELECT memory_b FROM conflicts WHERE status = 'open'",
            )?;
            let rows: Vec<String> = stmt
                .query_map([], |row| row.get::<_, String>(0))?
                .filter_map(|r| r.ok())
                .collect();
            drop(stmt);
            rows.into_iter().collect()
        };

        let mut stmt = conn.prepare(
            "SELECT claim_id, src, dst, rel_type, weight, created_at, \
             polarity, modality, valid_from, valid_to, extractor, confidence_band, \
             source_memory_rid, namespace, grounding \
             FROM claims WHERE source_memory_rid = ?1 AND tombstoned = 0 \
             ORDER BY created_at DESC, claim_id ASC",
        )?;
        let claims = stmt
            .query_map(params![rid], |row| {
                let claim_id: String = row.get(0)?;
                let polarity: i32 = row.get(6)?;
                let valid_to: Option<f64> = row.get(9)?;
                let source_rid: Option<String> = row.get(12)?;
                let status = if polarity == -1 {
                    "negative"
                } else if let Some(vt) = valid_to {
                    if vt < now {
                        "historical"
                    } else {
                        "superseded"
                    }
                } else if conflict_rids.contains(&claim_id)
                    || source_rid
                        .as_ref()
                        .map_or(false, |r| conflict_rids.contains(r))
                {
                    "conflicted"
                } else {
                    "active"
                };
                Ok(serde_json::json!({
                    "claim_id": claim_id,
                    "src": row.get::<_, String>(1)?,
                    "dst": row.get::<_, String>(2)?,
                    "rel_type": row.get::<_, String>(3)?,
                    "weight": row.get::<_, f64>(4)?,
                    "created_at": row.get::<_, f64>(5)?,
                    "polarity": polarity,
                    "modality": row.get::<_, String>(7)?,
                    "valid_from": row.get::<_, Option<f64>>(8)?,
                    "valid_to": valid_to,
                    "extractor": row.get::<_, String>(10)?,
                    "confidence_band": row.get::<_, String>(11)?,
                    "source_memory_rid": source_rid,
                    "namespace": row.get::<_, String>(13)?,
                    "grounding": row.get::<_, i64>(14)?,
                    "status_suggestion": status,
                }))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(claims)
    }

    /// Every correction ever applied to `rid`, oldest first (revision 1
    /// first). Empty for a memory that was never corrected, and empty for an
    /// unknown rid: an inspector asking about a rid it just listed should
    /// not have to distinguish "never corrected" from "gone".
    pub fn revision_history(&self, rid: &str) -> Result<Vec<RevisionEntry>> {
        let rows: Vec<RevisionRow> = {
            let conn = self.read_conn();
            let mut stmt = conn.prepare(
                "SELECT revision_id, rid, revision_num, prior_text, prior_metadata, \
                 prior_importance, prior_valence, reason, applied_at, origin_actor \
                 FROM record_revisions WHERE rid = ?1 ORDER BY revision_num ASC",
            )?;
            let rows = stmt
                .query_map(params![rid], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, i64>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, String>(4)?,
                        row.get::<_, f64>(5)?,
                        row.get::<_, f64>(6)?,
                        row.get::<_, String>(7)?,
                        row.get::<_, f64>(8)?,
                        row.get::<_, String>(9)?,
                    ))
                })?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            rows
        };
        let mut out = Vec::with_capacity(rows.len());
        for (
            revision_id,
            rid,
            revision_num,
            prior_text,
            prior_metadata,
            prior_importance,
            prior_valence,
            reason,
            applied_at,
            origin_actor,
        ) in rows
        {
            // Decrypt on hydration (see module docs); parse after decrypting.
            let prior_text = self.decrypt_text(&prior_text)?;
            let metadata_plain = self.decrypt_text(&prior_metadata)?;
            let prior_metadata =
                serde_json::from_str(&metadata_plain).unwrap_or(serde_json::Value::Null);
            out.push(RevisionEntry {
                revision_id,
                rid,
                revision_num,
                prior_text,
                prior_metadata,
                prior_importance,
                prior_valence,
                reason,
                applied_at,
                origin_actor,
            });
        }
        Ok(out)
    }

    /// The stored entity names linked to `rid`, sorted, exactly as the
    /// `memory_entities` table holds them: the same links the Memory Atlas
    /// draws and `entity_profile` counts. Empty for an unlinked or unknown
    /// rid.
    pub fn memory_entities(&self, rid: &str) -> Result<Vec<String>> {
        let conn = self.read_conn();
        let mut stmt = conn.prepare(
            "SELECT entity_name FROM memory_entities WHERE memory_rid = ?1 ORDER BY entity_name",
        )?;
        let names = stmt
            .query_map(params![rid], |row| row.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(names)
    }
}
