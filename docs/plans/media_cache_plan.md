# Media Cache Plan

Cache API responses (character data + title) per media to avoid redundant API calls.
User list fetching is never cached — only the per-media character data is.

---

## Table of Contents

1. [What Gets Cached](#what-gets-cached)
2. [Cache Key Design](#cache-key-design)
3. [Storage: SQLite BLOBs](#storage-sqlite-blobs)
4. [New Module: media_cache.rs](#new-module-srcmedia_cache-rs)
5. [Integration into main.rs](#integration-into-mainrs)
6. [Logging](#logging)
7. [Tests](#tests)
8. [End-to-End Flows](#end-to-end-flows)
9. [Files Changed](#files-changed)
10. [Dependencies](#dependencies)
11. [What Stays the Same](#what-stays-the-same)

---

## What Gets Cached

For each media (VN or anime/manga), we cache:

- **The media title** (Japanese/native preferred, same title-selection logic currently in
  `generate_vndb_dict()` and `generate_anilist_dict()`)
- **The `CharacterData` struct** — all characters with their metadata (name, role, traits,
  description, aliases, etc.)

What is **NOT** stored in the media cache:

- `image_bytes` and `image_ext` on each `Character` — these are always `None` in the
  cached data. Images come from the existing `ImageCache` on demand.
- User list results (`fetch_user_playing_list`, `fetch_user_current_list`) — these are
  always fetched fresh so the user sees their current playing/watching status.

### Why this separation works

`spoiler_level` and `honorifics` do not affect `CharacterData`. They are parameters to
`DictBuilder`, which runs *after* the data is fetched. This means a single cached entry
per media serves every combination of spoiler level and honorific settings.

On a cache hit we still call `download_images_concurrent()`, but every individual image
lookup goes through the existing `ImageCache` — which returns bytes from disk with zero
HTTP requests. The result is: **zero API calls on a fully warm cache**.

---

## Cache Key Design

| Source  | Key format                         | Example               |
|---------|------------------------------------|-----------------------|
| VNDB    | `vndb:{vn_id}`                     | `vndb:v17`            |
| AniList | `anilist:{media_id}:{media_type}`  | `anilist:9253:ANIME`  |

No `spoiler_level` or `honorifics` in the key — those are applied downstream by
`DictBuilder`, not during the API fetch. One cached entry serves all setting combinations.

The `media_type` is included for AniList because the same numeric ID can refer to
different media when the type differs (anime vs manga). VNDB IDs are globally unique
so only the VN ID is needed.

---

## Storage: SQLite BLOBs

Everything lives in a single SQLite database file — no separate files on disk, no shard
directories. The cached payload (title + serialized `CharacterData`, typically 50–200KB
without images) is stored as a BLOB column.

This is simpler than the current `ImageCache` approach of SQLite metadata + flat files.
No atomic rename dance, no shard directories, no missing-file self-healing.

### Schema

```sql
CREATE TABLE IF NOT EXISTS media (
    cache_key   TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    data        BLOB NOT NULL,       -- serde_json bytes of CharacterData (image_bytes = None)
    size_bytes  INTEGER NOT NULL,    -- length of the data BLOB in bytes
    created_at  INTEGER NOT NULL,    -- unix epoch seconds
    hit_count   INTEGER NOT NULL DEFAULT 0,
    last_hit_at INTEGER NOT NULL     -- unix epoch seconds
);
CREATE INDEX IF NOT EXISTS idx_media_hit_count ON media(hit_count);
```

### Constants

| Constant          | Value     | Rationale                                                    |
|-------------------|-----------|--------------------------------------------------------------|
| `MAX_CACHE_BYTES` | 1 GB      | Text-only data is small. ~200KB/entry = ~5,000 entries.      |
| `EVICT_FRACTION`  | 0.35      | Match existing image cache behavior.                         |
| `MAX_AGE_SECS`    | 2,592,000 (30 days) | Character data changes infrequently. 30 days is acceptable.  |

1 GB is generous for text-only data. At ~200KB per entry (a large VN with 50+ characters
and full trait/description data), that allows ~5,000 cached media entries before eviction
is triggered — far more than any realistic usage. Rate limiting (tower_governor) is the
correct defense against abuse, not a generous cache size.

### Why BLOBs instead of files

1. **Simpler code** — no directory creation, no atomic rename, no tmp files, no retry
   logic, no shard calculation, no file-missing cleanup.
2. **Atomic writes** — SQLite transactions are atomic. No partial-write corruption risk.
3. **Single file** — the entire cache is one `.db` file. Easy to inspect, back up, delete.
4. **Right size** — cached payloads are 50–200KB. SQLite handles BLOBs under 1MB with
   no performance issues. This would be different if we were storing images (10–100KB
   each, thousands of entries), but for media metadata the count is low and size is small.

---

## New Module: `src/media_cache.rs`

### Struct

```rust
#[derive(Clone)]
pub struct MediaCache {
    inner: Arc<MediaCacheInner>,
}

struct MediaCacheInner {
    /// SQLite connection (single-writer, serialized via Mutex).
    db: Mutex<Connection>,           // std::sync::Mutex<rusqlite::Connection>
    /// Running total of cached bytes (kept in sync with DB).
    total_bytes: AtomicU64,
    /// Prevents concurrent eviction tasks from being spawned.
    evicting: AtomicBool,
}
```

#### Call site 4: `generate_dict_from_usernames()` AniList loop (main.rs:696)

**Before** (lines 696–738):
```rust
"anilist" => {
    let media_id: i32 = match entry.id.parse() { ... };
    let media_type = match entry.media_type.as_str() { ... };
    let client = AnilistClient::with_client(state.http_client.clone());
    match client.fetch_characters(media_id, media_type).await {
        Ok((mut char_data, media_title)) => {
            let title = if !media_title.is_empty() { media_title } else { game_title.clone() };
            download_images_concurrent(&mut char_data, &state.http_client, &state.image_cache, 6).await;
            for character in char_data.all_characters() {
                builder.add_character(character, &title);
            }
        }
        Err(e) => { warn!(...); }
    }
    tokio::time::sleep(Duration::from_millis(700)).await;
}
```

**After**:
```rust
let (game_title, mut char_data, _cached) = fetch_vndb_cached(vn_id, state).await?;
download_images_concurrent(&mut char_data, &state.http_client, &state.image_cache, 8).await;
```

Everything below (DictBuilder creation, add_character loop, export) stays the same.

#### Call site 2: `generate_anilist_dict()` (main.rs:996)

**Before** (lines 1004–1015):
```rust
let client = AnilistClient::with_client(state.http_client.clone());
let (mut char_data, media_title) = client.fetch_characters(media_id, media_type).await?;
let game_title = if !media_title.is_empty() { media_title } else { format!("AniList {}", media_id) };
download_images_concurrent(&mut char_data, &state.http_client, &state.image_cache, 6).await;
```

**After**:
```rust
let (game_title, mut char_data, _cached) = fetch_anilist_cached(media_id, media_type, state).await?;
download_images_concurrent(&mut char_data, &state.http_client, &state.image_cache, 6).await;
```

#### Call site 3: `generate_dict_from_usernames()` VNDB loop (main.rs:661)

**Before** (lines 662–693):
```rust
"vndb" => {
    let client = VndbClient::with_client(state.http_client.clone());
    let title = match client.fetch_vn_title(&entry.id).await {
        Ok((romaji, original)) => {
            if !original.is_empty() { original } else { romaji }
        }
        Err(_) => game_title.clone(),
    };
    match client.fetch_characters(&entry.id).await {
        Ok(mut char_data) => {
            download_images_concurrent(&mut char_data, &state.http_client, &state.image_cache, 8).await;
            for character in char_data.all_characters() {
                builder.add_character(character, &title);
            }
        }
        Err(e) => { warn!(...); }
    }
    tokio::time::sleep(Duration::from_millis(200)).await;
}
```

**After**:
```rust
"vndb" => {
    match fetch_vndb_cached(&entry.id, state).await {
        Ok((title, mut char_data, cached)) => {
            download_images_concurrent(
                &mut char_data, &state.http_client, &state.image_cache, 8,
            ).await;
            for character in char_data.all_characters() {
                builder.add_character(character, &title);
            }
            // Only sleep on cache miss (API call was made, respect rate limit)
            if !cached {
                tokio::time::sleep(Duration::from_millis(200)).await;
            }
        }
        Err(e) => {
            warn!(vn_id = %entry.id, error = %e, "Failed to fetch VNDB characters");
        }
    }
}
```

**After**:
```rust
"anilist" => {
    let media_id: i32 = match entry.id.parse() {
        Ok(id) => id,
        Err(_) => { warn!(id = %entry.id, "Invalid AniList media ID"); continue; }
    };
    let media_type = match entry.media_type.as_str() {
        "anime" => "ANIME",
        "manga" => "MANGA",
        _ => "ANIME",
    };
    match fetch_anilist_cached(media_id, media_type, state).await {
        Ok((title, mut char_data, cached)) => {
            download_images_concurrent(
                &mut char_data, &state.http_client, &state.image_cache, 6,
            ).await;
            for character in char_data.all_characters() {
                builder.add_character(character, &title);
            }
            // Only sleep on cache miss (API call was made, respect rate limit)
            if !cached {
                tokio::time::sleep(Duration::from_millis(700)).await;
            }
        }
        Err(e) => {
            warn!(media_id = %entry.id, error = %e, "Failed to fetch AniList characters");
        }
    }
}
```

---

## Logging

All logging uses the `tracing` crate (already a dependency).

| Event | Level | Fields | Example message |
|-------|-------|--------|-----------------|
| Cache hit | `info!` | key, size_bytes | `"Media cache hit"` |
| Cache miss | `info!` | key | `"Media cache miss"` |
| Cache store | `info!` | key, size_bytes | `"Media cache stored"` |
| TTL expiry | `info!` | key, age_days | `"Media cache entry expired"` |
| Eviction | `info!` | evicted_count, freed_mb | `"Media cache eviction complete"` |
| Corrupt entry | `warn!` | key, error | `"Corrupt cache entry, removing"` |
| Serialization error | `warn!` | key, error | `"Failed to serialize cache entry"` |
| Cache opened | `info!` | total_mb, path | `"Media cache opened"` |

---

## Tests

All tests go in a `#[cfg(test)] mod tests` block inside `media_cache.rs`.
Use `tempfile::tempdir()` for isolated test directories (already a dev-dependency).

### Test list

**`test_put_and_get`** — Round-trip: create a `CharacterData` with a few characters,
`put()` it, `get()` it back, verify title and character count/fields match.

**`test_get_miss`** — `get()` on an unknown key returns `None`.

**`test_total_bytes_tracking`** — Put two entries, verify `total_bytes()` equals
the sum of their serialized sizes.

**`test_hit_count_increments`** — Put an entry, call `get()` 3 times, verify
`hit_count = 3` in the DB.

**`test_replace_preserves_hit_count`** — Put an entry, `get()` it twice (hit_count=2),
then `put()` again with updated data. Verify `hit_count` is still 2 (UPSERT preserves).

**`test_replace_adjusts_total_bytes`** — Put an entry (100 bytes), replace with larger
data (250 bytes), verify `total_bytes = 250`. Replace with smaller (50 bytes), verify
`total_bytes = 50`.

**`test_ttl_expiry`** — Insert an entry with `created_at` set to 31 days ago (requires
either a test helper that writes directly to DB, or exposing `created_at` override in
`put()`). Call `get()`, verify it returns `None` and the row is deleted.

**`test_corrupt_data_cleanup`** — Insert a row with invalid BLOB data directly via SQL.
Call `get()`, verify it returns `None`, the row is deleted, and `total_bytes` is adjusted.

### Test helper

```rust
fn make_test_char_data() -> CharacterData {
    let mut data = CharacterData::new();
    data.main.push(Character {
        id: "c1".to_string(),
        name: "Test Character".to_string(),
        name_original: "テスト".to_string(),
        role: "main".to_string(),
        // ... all other fields None/empty
    });
    data
}
```

---

## End-to-End Flows

### Cache Hit Flow

```
Request: GET /api/yomitan-dict?source=vndb&id=v17&spoiler_level=1&honorifics=true

1. media_cache.get("vndb:v17")                                  [1 SQLite query]
   → HIT: returns (title, char_data) with image_bytes = None

2. download_images_concurrent(&mut char_data, ..., 8)           [N image_cache lookups]
   For each character with image_url:
     image_cache.get(url)                                        [1 SQLite query + BLOB read per image]
     → HIT: returns (bytes, ext)
     → Populates character.image_bytes and character.image_ext

3. DictBuilder::new(spoiler_level=1, honorifics=true)
4. for character in char_data.all_characters():
       builder.add_character(character, &title)
5. builder.export_bytes() → ZIP bytes

Result: 0 API calls. 0 HTTP requests. All data from SQLite.
```

### Cache Miss Flow

```
Request: GET /api/yomitan-dict?source=vndb&id=v17&spoiler_level=1&honorifics=true

1. media_cache.get("vndb:v17")                                  [1 SQLite query]
   → MISS

2. fetch_vn_title("v17")                                        [1 VNDB API call]
   → (romaji_title, original_title) → pick title

3. fetch_characters("v17")                                      [1+ VNDB API calls, paginated]
   → CharacterData with all characters

4. Clear image_bytes/image_ext on all characters
5. media_cache.put("vndb:v17", title, char_data)                [1 SQLite write]

6. download_images_concurrent(&mut char_data, ..., 8)
   For each character with image_url:
     image_cache.get(url) → MISS (first time)
     → HTTP download → resize → image_cache.put()
     → Populates character.image_bytes and character.image_ext

7. DictBuilder::new(spoiler_level=1, honorifics=true)
8. for character: builder.add_character(character, &title)
9. builder.export_bytes() → ZIP bytes

Result: Next request for v17 (any spoiler_level/honorifics combo) hits cache.
        Next request for same images (even from different media) hits image cache.
```

### Username Flow (multiple media)

```
Request: GET /api/generate-stream?vndb_user=foo&anilist_user=bar&spoiler_level=0

1. fetch_user_playing_list("foo")                               [VNDB API — always fresh]
   → [UserMediaEntry{id: "v17", ...}, UserMediaEntry{id: "v24", ...}]

2. fetch_user_current_list("bar")                               [AniList API — always fresh]
   → [UserMediaEntry{id: "9253", media_type: "anime", ...}]

3. Deduplicate by (source, id)

4. For each media entry:
   a. fetch_vndb_cached("v17", state) or fetch_anilist_cached(9253, "ANIME", state)
      → cache hit: instant (SQLite read), no sleep
      → cache miss: full API fetch + cache store + rate-limit sleep
   b. download_images_concurrent(char_data)
      → per-image: image_cache hit (disk) or miss (HTTP download)
   c. builder.add_character(character, &title) for each character

5. builder.export_bytes() → ZIP
```

---

## Files Changed

| File | Action | Description |
|------|--------|-------------|
| `src/media_cache.rs` | **NEW** | ~250 lines. MediaCache struct, open/get/put/evict, tests. |
| `src/main.rs` | **EDIT** | Add `mod media_cache`, add `media_cache` field to AppState, add `MediaCache::open()` to `AppState::new()`, add `fetch_vndb_cached()` and `fetch_anilist_cached()` wrapper functions, update 4 call sites. |

No changes to `models.rs` — `CharacterData` and `Character` already have the right
derives and fields.

No changes to `image_cache.rs` — it continues to work exactly as-is.

---

## Dependencies

**None new.** All crates used by `media_cache.rs` are already in `Cargo.toml`:

| Crate | Usage in media_cache |
|-------|---------------------|
| `rusqlite` (with `bundled` feature) | SQLite connection, queries, BLOBs |
| `serde_json` | Serialize/deserialize `CharacterData` |
| `tokio` | `spawn_blocking`, `spawn` for background eviction |
| `tracing` | `info!`, `warn!` logging |
| `tempfile` (dev-dependency) | Test isolation |

`sha2` is not needed — cache keys are used directly as the PRIMARY KEY (they're already
short, human-readable strings like `vndb:v17`). No hashing required.

---

## What Stays the Same

- **`ImageCache`** (`image_cache.rs`) — continues to work exactly as-is. Stores
  processed image bytes (after resize) on disk. Used by `download_images_concurrent()`.
- **User list fetching** — `fetch_user_playing_list()` and `fetch_user_current_list()`
  always hit the live API. Users see their real-time playing/watching status.
- **Download token store** — unchanged (in-memory HashMap with 5-minute TTL).
- **Rate limiting** — unchanged (tower_governor middleware).
- **`DictBuilder`** — unchanged. Still receives `spoiler_level` and `honorifics` as
  constructor parameters. Still processes `CharacterData` the same way.
- **Frontend** — unchanged. No new UI for cache management.
- **All other modules** — `vndb_client.rs`, `anilist_client.rs`, `name_parser.rs`,
  `content_builder.rs`, `image_handler.rs`, `kana.rs`, `dict_builder.rs` — untouched.

---

See [media_cache_extension.md](media_cache_extension.md) for a future plan to migrate ImageCache to SQLite BLOBs.
