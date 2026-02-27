# EC2 Image & Character Data Caching Plan (Revised)

## What's Already Done

The codebase has implemented a significant chunk of the original caching plan:

| Original Phase | Status | Implementation |
|---|---|---|
| 1. Kill base64 round-trip | ✅ Done | `image_base64` field removed from `Character`. Clients no longer have `fetch_image_as_base64()`. All image downloading centralized in `fetch_and_cache_image()` in main.rs. |
| 2. Shared HTTP client | ✅ Done | `AppState.http_client` shared across all handlers. Clients take `with_client(client)` — no more `new()`. |
| 3. In-memory image cache | ✅ Done | `AppState.image_cache: Cache<String, ImageCacheEntry>` — moka with 10K entry cap, 24h TTL. `fetch_and_cache_image()` checks cache before downloading. |
| 4. Concurrent image downloads | ✅ Done | `download_images_concurrent()` uses `futures::stream::buffer_unordered()` — 8 concurrent for VNDB, 6 for AniList. |
| 5. ZIP-level caching | ✅ Done | `AppState.zip_cache: Cache<String, ZipCacheEntry>` — moka with 200 entry cap, 15min TTL. SHA-256 keyed on query params. Skipped for SSE streaming. |
| 6. Store images uncompressed | ✅ Done | `dict_builder.rs` uses `CompressionMethod::Stored` for images, `Deflated` for JSON. |
| Image resizing | ✅ Done | `ImageHandler::resize_image()` resizes to max dimensions, converts to WebP. Applied in `fetch_and_cache_image()` before caching. |
| HTTP 429 retry | ✅ Done (bonus) | Both clients have `send_with_retry()` with exponential backoff + Retry-After header support. |
| Structured logging | ✅ Done (bonus) | `tracing` + `tracing-subscriber` with env-filter. `warn!` macros throughout. |
| URL encoding | ✅ Done (bonus) | `urlencoding` crate for download URLs. |
| Per-image timeout | ✅ Done (bonus) | 10s timeout per image download in `fetch_and_cache_image()`. |

## What's Left — Remaining Egress Reduction Opportunities

The big wins are already captured. What remains are second-order optimizations that matter more as traffic scales.

---

### 1. API Response Caching

**Status**: Not implemented. Every request still hits VNDB/AniList APIs for character lists and titles, even when the same VN/anime was queried minutes ago.

**Why it matters on EC2**: API calls are small individually (~2-10KB each), but a username-based request across 10 titles makes 25-40 API calls. The real cost isn't bytes — it's latency (each call is 100-300ms) and rate limit pressure. Caching API responses means fewer outbound connections and faster generation even on cache-miss for images.

**Proposed approach**:

Add a third moka cache to `AppState`:

```rust
struct AppState {
    // ... existing fields ...
    /// API response cache: key → serialized response.
    /// Keyed by "vndb:chars:{vn_id}", "vndb:title:{vn_id}",
    /// "anilist:chars:{media_id}:{media_type}", "anilist:ulist:{user_hash}", etc.
    api_cache: Cache<String, Arc<serde_json::Value>>,
}
```

Config: 5,000 entry cap, tiered TTLs applied via `policy::Expiry`:
- Character data: 2 hours (stable for released media)
- Titles: 24 hours (basically never change)
- User lists: 10 minutes (users actively update these)

Integration — wrap the client calls in the orchestration functions:

```rust
// In generate_vndb_dict / generate_dict_from_usernames
let cache_key = format!("vndb:chars:{}", vn_id);
let char_data: CharacterData = match state.api_cache.get(&cache_key).await {
    Some(cached) => serde_json::from_value((*cached).clone()).unwrap(),
    None => {
        let data = client.fetch_characters(vn_id).await?;
        let value = Arc::new(serde_json::to_value(&data).unwrap());
        state.api_cache.insert(cache_key, value).await;
        data
    }
};
```

Same pattern for `fetch_vn_title()`, `fetch_user_current_list()`, and `fetch_characters()` on the AniList side.

**Estimated impact**: Eliminates ~80% of API calls for repeat/overlapping requests. Cuts 2-8 seconds off username-based generation when character data is cached.

---

### 2. Disk-Backed Image Cache

**Status**: Not implemented. The comment on `ImageCacheEntry` says "Disk-backed via moka" but it's actually pure in-memory. Process restart = cold cache = re-download everything.

**Why it matters on EC2**: EC2 instances restart (deploys, spot reclamation, scaling events). A cold cache means the first N requests after restart re-download every image. With EBS gp3 at $0.08/GB/month vs egress at $0.09/GB, storing 5GB of cached images on disk costs $0.40/month and saves potentially gigabytes of re-downloads.

**Proposed approach**:

Wrap the moka cache with a disk fallback layer. Keep it simple — no custom eviction daemon, just filesystem TTL via file modification time.

```rust
struct DiskImageCache {
    dir: PathBuf,  // e.g. /var/cache/yomitan/images
}

impl DiskImageCache {
    /// Read cached image from disk. Returns None if missing or expired.
    async fn get(&self, url: &str) -> Option<(Vec<u8>, String)> {
        let hash = sha256_hex(url);
        let meta_path = self.dir.join(format!("{}.meta", hash));
        let meta: CacheMeta = read_json(&meta_path).await.ok()?;
        if meta.is_expired() { return None; }
        let data_path = self.dir.join(format!("{}.{}", hash, meta.ext));
        let bytes = tokio::fs::read(&data_path).await.ok()?;
        Some((bytes, meta.ext))
    }

    /// Write image to disk atomically (write .tmp then rename).
    async fn put(&self, url: &str, bytes: &[u8], ext: &str) {
        let hash = sha256_hex(url);
        let tmp = self.dir.join(format!("{}.tmp", hash));
        let final_path = self.dir.join(format!("{}.{}", hash, ext));
        tokio::fs::write(&tmp, bytes).await.ok();
        tokio::fs::rename(&tmp, &final_path).await.ok();
        // Write metadata
        let meta = CacheMeta { ext: ext.to_string(), ttl_secs: 604800, created: now() };
        write_json(&self.dir.join(format!("{}.meta", hash)), &meta).await.ok();
    }
}
```

Modify `fetch_and_cache_image()` to check: moka → disk → HTTP, and write back to both on miss.

Disk cleanup: a background tokio task every 30 minutes that walks the directory and deletes files older than 7 days. Simple `tokio::fs::read_dir` + `metadata().modified()` check.

**Estimated impact**: Eliminates cold-start re-download penalty. First request after restart serves from disk (~1ms) instead of HTTP (~200ms per image).

**New dependency**: None. Uses `tokio::fs` (already available) and `sha2` (already in Cargo.toml).

---

### 3. Request Coalescing (In-Flight Deduplication)

**Status**: Not implemented. If two users request the same VN dictionary simultaneously, both do the full pipeline independently — duplicate API calls, duplicate image downloads (though the second will mostly hit the image cache after the first populates it).

**Why it matters on EC2**: The ZIP cache has a 15-minute TTL, so within that window repeat requests are instant. But two truly simultaneous requests (before either finishes) both do full work. This matters most for Yomitan auto-updates, where multiple browser instances might trigger at the same moment.

**Proposed approach**:

Use `tokio::sync::broadcast` channels keyed by the ZIP cache key:

```rust
use dashmap::DashMap;
use tokio::sync::broadcast;

struct AppState {
    // ... existing fields ...
    /// In-flight generation tracker. Key = zip_cache_key.
    /// First request creates a broadcast channel, subsequent requests subscribe.
    in_flight: DashMap<String, broadcast::Sender<Result<Vec<u8>, String>>>,
}
```

In `generate_dict` / `generate_dict_from_usernames`:

```rust
let cache_key = zip_cache_key(...);

// 1. Check ZIP cache (already done)
if let Some(cached) = state.zip_cache.get(&cache_key).await { return Ok(cached); }

// 2. Check if someone else is already generating this
if let Some(sender) = state.in_flight.get(&cache_key) {
    let mut rx = sender.subscribe();
    drop(sender); // release DashMap read lock
    return rx.recv().await.unwrap_or(Err("Generation failed".into()));
}

// 3. We're first — create channel and do the work
let (tx, _) = broadcast::channel(1);
state.in_flight.insert(cache_key.clone(), tx.clone());

let result = do_actual_generation(...).await;

// 4. Broadcast result to waiters, clean up
let _ = tx.send(result.clone());
state.in_flight.remove(&cache_key);

result
```

**Estimated impact**: Prevents duplicate work under concurrent load. Saves 1x full generation cost per duplicate simultaneous request.

**New dependency**: `dashmap` crate.

---

### 4. Differentiated ZIP Cache TTLs

**Status**: Partially done. The ZIP cache uses a flat 15-minute TTL for everything. But single-media requests (e.g., `?source=vndb&id=v17`) produce deterministic output — the character list for a released VN doesn't change hour to hour. These could safely cache for much longer.

**Why it matters**: A longer TTL for single-media means Yomitan auto-update checks for specific VNs/anime hit the cache instead of regenerating. Username-based requests need shorter TTLs because the user's playing list changes.

**Proposed approach**:

Moka supports per-entry TTL via `policy::Expiry`. Replace the flat TTL:

```rust
struct ZipExpiry;

impl Expiry<String, ZipCacheEntry> for ZipExpiry {
    fn expire_after_create(
        &self, key: &String, _value: &ZipCacheEntry, _created_at: Instant,
    ) -> Option<Duration> {
        if key.starts_with("single:") {
            Some(Duration::from_secs(3600))  // 1 hour for single-media
        } else {
            Some(Duration::from_secs(900))   // 15 min for username-based
        }
    }
}
```

Adjust `zip_cache_key()` to prefix keys with `single:` or `user:` so the expiry policy can distinguish them.

**Estimated impact**: ~4x longer cache window for the most common Yomitan auto-update pattern (single-media dictionaries).

**New dependency**: None.

---

### 5. Image Cache Sizing Fix

**Status**: The current image cache uses `max_capacity(10_000)` which counts entries, not bytes. Post-resize WebP images are ~5-15KB each, so 10K entries ≈ 50-150MB — reasonable. But the comment says "500MB max" which doesn't match the implementation.

**Proposed fix**: Use moka's `weigher` to count by byte size instead of entry count:

```rust
image_cache: Cache::builder()
    .weigher(|_key: &String, value: &ImageCacheEntry| -> u32 {
        // Weight = approximate memory footprint in bytes
        (value.0.len() + value.1.len() + 64) as u32  // 64 bytes overhead estimate
    })
    .max_capacity(500 * 1024 * 1024)  // 500MB actual byte limit
    .time_to_live(std::time::Duration::from_secs(86400))
    .build(),
```

This ensures the cache actually respects a byte budget rather than an arbitrary entry count.

**Estimated impact**: More predictable memory usage. Prevents OOM if images are unexpectedly large.

---

### 6. Cache Stats Endpoint

**Status**: Not implemented. No visibility into cache effectiveness.

**Why it matters**: Without metrics, you can't tell if the cache is actually saving egress or if the TTLs need tuning.

**Proposed approach**: Add `GET /api/cache-stats`:

```json
{
  "image_cache": { "entry_count": 1234, "hit_rate": 0.87 },
  "zip_cache": { "entry_count": 12, "hit_rate": 0.62 },
  "api_cache": { "entry_count": 89, "hit_rate": 0.74 }
}
```

Moka exposes `entry_count()` and `run_pending_tasks()` but not hit/miss counters natively. Add `AtomicU64` counters in `AppState` and increment them in `fetch_and_cache_image()` and the ZIP cache check paths.

---

## Revised Priority Order

Only listing what's NOT yet done:

| Item | Effort | Egress Impact | Latency Impact | Notes |
|---|---|---|---|---|
| 5. Image cache sizing fix | Tiny | None | None | Correctness fix, 5-line change |
| 4. Differentiated ZIP TTLs | Small | Medium | High | Longer cache for stable content |
| 1. API response caching | Medium | Low-Medium | High | Fewer API round-trips, faster generation |
| 2. Disk-backed image cache | Medium | High | Medium | Survives restarts, biggest remaining egress win |
| 6. Cache stats endpoint | Small | None | None | Observability, needed to tune TTLs |
| 3. Request coalescing | Medium | Low | Medium | Only matters under concurrent load |

## New Dependencies Needed

| Crate | Purpose | For Item |
|---|---|---|
| `dashmap` | Concurrent map for in-flight request tracking | Request coalescing (#3) |

Everything else uses crates already in `Cargo.toml`.

---

## Egress Math (Updated)

With the current implementation (image cache + ZIP cache + concurrent downloads + image resize), the egress profile looks like:

| Scenario | First Request | Repeat (within 15min) | Repeat (after 15min, images cached) |
|---|---|---|---|
| Single VN, 50 chars | ~1MB images + ~15KB API | 0 (ZIP cache hit) | ~15KB API only (images cached 24h) |
| Username, 10 titles | ~8-20MB images + ~100KB API | 0 (ZIP cache hit) | ~100KB API only |
| Yomitan auto-update | Same as first | 0 (ZIP cache hit) | Same as "after 15min" |

The remaining egress gap is:
1. **Cold starts** (no disk cache → re-download all images) — fixed by item #2
2. **API calls on every generation** (even when character data hasn't changed) — fixed by item #1
3. **15min ZIP TTL too short for stable single-media content** — fixed by item #4


Kanji detection only covers CJK Unified Ideographs (U+4E00–U+9FFF) and Extension A (U+3400–U+4DBF). CJK Extension B through G (U+20000+) are not detected as kanji, so rare characters used in some names would be treated as kana and get the wrong reading path.

The romaji→hiragana table is comprehensive but not exhaustive. Missing mappings (e.g., "dya", "tya", "fa", "fi", "fe", "fo", long vowels like "ou" → "おう") cause those characters to pass through unchanged, producing garbage readings.
We use Hepburn, do we need to do this?

haracters without name_original are silently dropped. No warning, no log. If VNDB returns a character with only a romanized name and no Japanese original, it simply doesn't appear in the dictionary.

Concurrent image downloads are hardcoded at 8 for VNDB and 6 for AniList. These aren't configurable and don't adapt to actual rate limit responses.

Binds to 0.0.0.0:3000 unconditionally. No configurable port. Can we set via env var?

static/ directory resolution uses CARGO_MANIFEST_DIR in debug builds, which is a compile-time constant. Moving the binary after compilation breaks static file serving in debug mode.

Write to the readme how to run this on a server with a custom domain and port, and how it works with docker.