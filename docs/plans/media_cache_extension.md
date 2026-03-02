# ImageCache SQLite BLOB Migration Plan

This is a follow-up to the media cache plan in `media_cache_plan.md`.

**This is a separate work item.** It simplifies `image_cache.rs` by removing all file
management code and storing image bytes directly in SQLite, matching the approach used
by the new `MediaCache`.

---

## Table of Contents

1. [Why Do This](#why-do-this)
2. [Current State of image_cache.rs](#current-state-of-image_cachrs)
3. [What Changes (High Level)](#what-changes-high-level)
4. [New Schema](#new-schema)
5. [Step-by-Step: Each Method Rewrite](#step-by-step-each-method-rewrite)
6. [Struct Changes](#struct-changes)
7. [Public API (Unchanged)](#public-api-unchanged)
8. [SQLite BLOB Performance](#sqlite-blob-performance)
9. [Migration Strategy](#migration-strategy)
10. [Test Changes](#test-changes)
11. [Estimated Impact](#estimated-impact)
12. [Files Changed](#files-changed)
13. [Checklist](#checklist)

---

## Why Do This

Right now `image_cache.rs` does two jobs:

1. **SQLite metadata** -- tracks which images are cached, their popularity, and TTL.
2. **Filesystem I/O** -- writes/reads actual image bytes as flat files in sharded
   subdirectories (`images/ab/ab1234...jpg`).

Job #2 accounts for ~200 lines of code and introduces complexity:
- Shard directory creation and management
- Atomic write pattern (write to `.tmp` file, then rename)
- Retry logic when a shard directory gets deleted by eviction mid-write
- File cleanup loops during eviction
- Self-healing when a file goes missing behind the cache's back

All of this goes away if we store the image bytes as a BLOB column in SQLite. The
`MediaCache` module (`media_cache.rs`) already uses this approach successfully for
API response data. Images after resize are small enough (10-100KB) that SQLite handles
them efficiently -- this is well within SQLite's recommended BLOB size range.

---

## Current State of image_cache.rs

The file is **530 lines** and works like this:

### Data flow today

```
put(url, bytes, ext)
  1. Hash the URL → 64-char hex string (e.g. "ab12cd34...")
  2. Pick shard directory from first 2 chars → "images/ab/"
  3. Create shard directory if it doesn't exist
  4. Write bytes to "images/ab/ab12cd34.tmp"
  5. Rename "ab12cd34.tmp" → "ab12cd34.jpg"
     - If rename fails because shard dir was deleted (race condition with eviction):
       re-create shard dir, retry rename
  6. Insert/update metadata row in SQLite

get(url)
  1. Hash the URL
  2. Query SQLite for file_path, ext, size, created_at
  3. Check TTL (6 months) — if expired, delete row AND file
  4. Read bytes from disk at file_path
     - If file is missing: log warning, delete row, fix byte counter
  5. Return bytes + ext
```

### Current SQLite schema

```sql
CREATE TABLE IF NOT EXISTS images (
    url_hash    TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    file_path   TEXT NOT NULL,      -- relative path like "ab/ab12cd34.jpg"
    size_bytes  INTEGER NOT NULL,
    ext         TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0,
    last_hit_at INTEGER NOT NULL
);
```

### Current struct

```rust
struct ImageCacheInner {
    db: Mutex<Connection>,
    images_dir: PathBuf,        // ← root directory for cached image files
    total_bytes: AtomicU64,
}
```

### Where the 200 lines of file I/O live

| Location | What it does | Lines |
|----------|-------------|-------|
| `open()` | Creates `images/` directory | ~5 |
| `get()` | Reads file from disk, self-heals on missing file | ~20 |
| `put()` | Shard dir creation, tmp write, atomic rename, retry | ~50 |
| `evict()` | Loops through entries, deletes files one by one | ~15 |
| Struct | `images_dir: PathBuf` field and its setup | ~5 |
| Error handling | 8 separate file-error branches throughout | ~100+ |

---

## What Changes (High Level)

**Before:** SQLite stores metadata, filesystem stores bytes.

**After:** SQLite stores everything. No filesystem I/O at all.

```
BEFORE                              AFTER
┌──────────────┐                    ┌──────────────────┐
│   SQLite DB  │                    │     SQLite DB     │
│  (metadata)  │                    │ (metadata + data) │
│              │                    │                   │
│  url_hash    │                    │  url_hash         │
│  url         │                    │  url              │
│  file_path ──┼──→ filesystem      │  data (BLOB) ←──── bytes stored here now
│  size_bytes  │                    │  size_bytes       │
│  ext         │                    │  ext              │
│  created_at  │                    │  created_at       │
│  hit_count   │                    │  hit_count        │
│  last_hit_at │                    │  last_hit_at      │
└──────────────┘                    └──────────────────┘
```

---

## New Schema

Replace `file_path TEXT NOT NULL` with `data BLOB NOT NULL`:

```sql
CREATE TABLE IF NOT EXISTS images (
    url_hash    TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    data        BLOB NOT NULL,       -- raw image bytes (after resize, typically 10-100KB)
    size_bytes  INTEGER NOT NULL,
    ext         TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0,
    last_hit_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hit_count ON images(hit_count);
```

That's the only schema change: one column swapped from `file_path TEXT` to `data BLOB`.

---

## Step-by-Step: Each Method Rewrite

### 1. `open(cache_dir)` — minor simplification

**What to remove:**
- The `images_dir` field and `create_dir_all()` call
- The `images_dir` variable entirely

**What to keep:**
- SQLite open, pragmas, table creation, total_bytes initialization

**Before** (current code, simplified):

```rust
pub fn open(cache_dir: &Path) -> Result<Self, String> {
    let images_dir = cache_dir.join("images");                      // REMOVE
    std::fs::create_dir_all(&images_dir)?;                          // REMOVE

    let db_path = images_dir.join("cache.db");                      // CHANGE path
    let conn = Connection::open(&db_path)?;

    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")?;
    conn.execute_batch("CREATE TABLE IF NOT EXISTS images (...)")?; // UPDATE schema

    let total: u64 = conn.query_row("SELECT COALESCE(SUM(size_bytes), 0) FROM images", ...)?;

    Ok(Self {
        inner: Arc::new(ImageCacheInner {
            db: Mutex::new(conn),
            images_dir,                                              // REMOVE
            total_bytes: AtomicU64::new(total),
        }),
    })
}
```

**After:**

```rust
pub fn open(cache_dir: &Path) -> Result<Self, String> {
    // Ensure cache directory exists (just the top-level dir, no images/ subdirectory)
    std::fs::create_dir_all(cache_dir)
        .map_err(|e| format!("Failed to create cache dir: {}", e))?;

    let db_path = cache_dir.join("image_cache.db");
    let conn = Connection::open(&db_path)
        .map_err(|e| format!("Failed to open cache DB: {}", e))?;

    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
        .map_err(|e| format!("Failed to set pragmas: {}", e))?;

    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS images (
            url_hash    TEXT PRIMARY KEY,
            url         TEXT NOT NULL,
            data        BLOB NOT NULL,
            size_bytes  INTEGER NOT NULL,
            ext         TEXT NOT NULL,
            created_at  INTEGER NOT NULL,
            hit_count   INTEGER NOT NULL DEFAULT 0,
            last_hit_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_hit_count ON images(hit_count);",
    )
    .map_err(|e| format!("Failed to create table: {}", e))?;

    let total: u64 = conn
        .query_row(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM images",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0);

    info!(
        total_mb = total / (1024 * 1024),
        path = %cache_dir.display(),
        "Image cache opened"
    );

    Ok(Self {
        inner: Arc::new(ImageCacheInner {
            db: Mutex::new(conn),
            total_bytes: AtomicU64::new(total),
        }),
    })
}
```

**Net change:** ~5 lines removed. DB path changes from `images/cache.db` to
`image_cache.db` (no subdirectory needed).

---

### 2. `get(url)` — major simplification

**What to remove:**
- The entire file read (`tokio::fs::read`)
- The entire self-healing branch (file missing → delete row, fix counter)
- The `file_path` from the SELECT query
- The file deletion on TTL expiry

**What to keep:**
- URL hashing
- SQLite query
- TTL check
- Hit count bump

**Before** (current code, 50 lines):

```rust
pub async fn get(&self, url: &str) -> Option<(Vec<u8>, String)> {
    let hash = url_hash(url);
    let db = self.inner.db.lock().await;

    // Query returns: file_path, ext, size_bytes, created_at
    let (file_path, ext, size_bytes, created_at) = db.query_row(...)?;

    // TTL check
    if now - created_at > MAX_AGE_SECS {
        db.execute("DELETE FROM images WHERE url_hash = ?1", ...);
        drop(db);
        saturating_sub(&self.inner.total_bytes, size_bytes as u64);
        let _ = tokio::fs::remove_file(&full_path).await;   // file cleanup
        return None;
    }

    // Bump hit_count
    db.execute("UPDATE images SET hit_count = hit_count + 1, ...", ...);
    drop(db);

    // Read from filesystem (the complex part)
    let full_path = self.inner.images_dir.join(&file_path);
    match tokio::fs::read(&full_path).await {
        Ok(bytes) => Some((bytes, ext)),
        Err(e) => {
            // Self-healing: file missing, clean up DB row
            warn!("Cache file missing, removing entry");
            let db = self.inner.db.lock().await;
            db.execute("DELETE FROM images WHERE url_hash = ?1", ...);
            drop(db);
            saturating_sub(&self.inner.total_bytes, size_bytes as u64);
            None
        }
    }
}
```

**After** (25 lines):

```rust
pub async fn get(&self, url: &str) -> Option<(Vec<u8>, String)> {
    let hash = url_hash(url);
    let db = self.inner.db.lock().await;

    // Query returns: data (BLOB), ext, size_bytes, created_at
    let result: Option<(Vec<u8>, String, i64, i64)> = db
        .query_row(
            "SELECT data, ext, size_bytes, created_at FROM images WHERE url_hash = ?1",
            params![hash],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .ok();

    let (data, ext, size_bytes, created_at) = result?;

    // TTL check: expire entries older than 6 months
    let now = now_secs();
    if now - created_at > MAX_AGE_SECS {
        let _ = db.execute("DELETE FROM images WHERE url_hash = ?1", params![hash]);
        drop(db);
        saturating_sub(&self.inner.total_bytes, size_bytes as u64);
        return None;
    }

    // Bump popularity
    let _ = db.execute(
        "UPDATE images SET hit_count = hit_count + 1, last_hit_at = ?1 WHERE url_hash = ?2",
        params![now, hash],
    );

    Some((data, ext))
}
```

**What's gone:**
- No `file_path` in the query -- `data` (the BLOB) replaces it
- No `tokio::fs::read()` -- data comes directly from SQLite
- No missing-file self-healing -- there's no file to go missing
- No file deletion on TTL expiry -- `DELETE` from SQLite removes everything

**Net change:** ~20 lines removed.

---

### 3. `put(url, bytes, ext)` — biggest simplification

This is where most of the file management complexity lives. All of the following goes away:

- Shard directory calculation (`&hash[..2]`)
- `create_dir_all` for shard directory
- Writing to a `.tmp` file
- Renaming `.tmp` to final name
- Retry logic when shard dir is deleted by concurrent eviction
- Error handling for each of those steps

**Before** (current code, ~100 lines):

```rust
pub async fn put(&self, url: &str, bytes: &[u8], ext: &str) {
    let hash = url_hash(url);
    let shard = &hash[..2];                                          // REMOVE all of this
    let file_name = format!("{}.{}", hash, ext);                     // REMOVE
    let rel_path = format!("{}/{}", shard, file_name);               // REMOVE
    let shard_dir = self.inner.images_dir.join(shard);               // REMOVE

    // Create shard directory                                         REMOVE
    if let Err(e) = tokio::fs::create_dir_all(&shard_dir).await { } // REMOVE

    // Atomic write: tmp → rename                                     REMOVE (~35 lines)
    let final_path = shard_dir.join(&file_name);                     // REMOVE
    let tmp_path = shard_dir.join(format!("{}.tmp", file_name));     // REMOVE
    tokio::fs::write(&tmp_path, bytes).await?;                       // REMOVE
    tokio::fs::rename(&tmp_path, &final_path).await?;                // REMOVE
    // ... plus retry logic for failed rename ...                     // REMOVE

    // SQLite insert (keep this part, but modify the INSERT)
    let size = bytes.len() as u64;
    let now = now_secs();
    let db = self.inner.db.lock().await;
    let old_size = db.query_row("SELECT size_bytes ...", ...);
    db.execute("INSERT INTO images ... ON CONFLICT DO UPDATE ...", ...);
    drop(db);

    // Byte counter adjustment (keep)
    // Eviction trigger (keep)
}
```

**After** (~40 lines):

```rust
pub async fn put(&self, url: &str, bytes: &[u8], ext: &str) {
    let hash = url_hash(url);
    let size = bytes.len() as u64;
    let now = now_secs();

    let db = self.inner.db.lock().await;

    // Check for existing entry to get old size for counter adjustment
    let old_size: Option<i64> = db
        .query_row(
            "SELECT size_bytes FROM images WHERE url_hash = ?1",
            params![hash],
            |row| row.get(0),
        )
        .ok();

    // Upsert: on conflict, update data/size/ext but preserve hit_count and last_hit_at
    let result = db.execute(
        "INSERT INTO images (url_hash, url, data, size_bytes, ext, created_at, hit_count, last_hit_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, 0, ?6)
         ON CONFLICT(url_hash) DO UPDATE SET
            data = excluded.data,
            size_bytes = excluded.size_bytes,
            ext = excluded.ext,
            created_at = excluded.created_at",
        params![hash, url, bytes, size as i64, ext, now],
    );
    drop(db);

    if result.is_err() {
        warn!("Failed to insert cache entry");
        return;
    }

    // Adjust byte counter: subtract old size (if replacing), add new size
    if let Some(old) = old_size {
        let old = old as u64;
        if size > old {
            self.inner.total_bytes.fetch_add(size - old, Ordering::Relaxed);
        } else {
            saturating_sub(&self.inner.total_bytes, old - size);
        }
    } else {
        self.inner.total_bytes.fetch_add(size, Ordering::Relaxed);
    }

    // Trigger eviction if over limit
    let current_total = self.inner.total_bytes.load(Ordering::Relaxed);
    if current_total > MAX_CACHE_BYTES {
        let cache = self.clone();
        tokio::spawn(async move {
            cache.evict().await;
        });
    }
}
```

**What's gone:**
- No shard calculation
- No directory creation
- No tmp file write
- No rename
- No rename retry
- No file-related error handling (5 separate error branches removed)

The BLOB bytes are passed directly as a parameter in the SQL INSERT. SQLite handles
atomicity through its transaction mechanism -- no tmp+rename needed.

**Net change:** ~45 lines removed.

---

### 4. `evict()` — moderate simplification

**What to remove:**
- The file deletion loop (`tokio::fs::remove_file` per entry)
- The `file_path` column from the SELECT query

**What to keep:**
- The popularity-based selection query
- The batch DELETE query
- The byte counter adjustment

**Before** (current code):

```rust
async fn evict(&self) {
    // Step 1: Query for least-popular entries (KEEP)
    let entries: Vec<(String, String, u64)> = {
        let db = self.inner.db.lock().await;
        // SELECT url_hash, file_path, size_bytes ...
        // ORDER BY hit_count ASC, last_hit_at ASC
        // LIMIT evict_count
    };

    // Step 2: Delete files one by one (REMOVE)
    for (hash, file_path, size) in &entries {
        let full_path = self.inner.images_dir.join(file_path);
        let _ = tokio::fs::remove_file(&full_path).await;          // REMOVE
        freed += size;
        deleted_hashes.push(hash.clone());
    }

    // Step 3: Batch DELETE from DB (KEEP)
    // Step 4: Update byte counter (KEEP)
}
```

**After:**

```rust
async fn evict(&self) {
    let db = self.inner.db.lock().await;

    let count: u64 = db
        .query_row("SELECT COUNT(*) FROM images", [], |row| row.get(0))
        .unwrap_or(0);

    if count == 0 {
        return;
    }

    let evict_count = ((count as f64) * EVICT_FRACTION).ceil() as u64;

    // Select the least popular entries
    let mut stmt = match db.prepare(
        "SELECT url_hash, size_bytes FROM images
         ORDER BY hit_count ASC, last_hit_at ASC
         LIMIT ?1",
    ) {
        Ok(s) => s,
        Err(e) => {
            warn!(error = %e, "Failed to prepare eviction query");
            return;
        }
    };

    let entries: Vec<(String, u64)> = stmt
        .query_map(params![evict_count], |row| {
            Ok((row.get(0)?, row.get::<_, i64>(1)? as u64))
        })
        .ok()
        .map(|rows| rows.filter_map(|r| r.ok()).collect())
        .unwrap_or_default();

    if entries.is_empty() {
        return;
    }

    // Batch DELETE — this removes both metadata AND data (BLOBs) in one query
    let hashes: Vec<String> = entries.iter().map(|(h, _)| h.clone()).collect();
    let freed: u64 = entries.iter().map(|(_, s)| s).sum();

    let placeholders: Vec<String> = (1..=hashes.len()).map(|i| format!("?{}", i)).collect();
    let sql = format!(
        "DELETE FROM images WHERE url_hash IN ({})",
        placeholders.join(",")
    );
    let params: Vec<&dyn rusqlite::types::ToSql> = hashes
        .iter()
        .map(|h| h as &dyn rusqlite::types::ToSql)
        .collect();
    let _ = db.execute(&sql, params.as_slice());

    drop(db);

    saturating_sub(&self.inner.total_bytes, freed);

    info!(
        evicted = hashes.len(),
        freed_mb = freed / (1024 * 1024),
        "Image cache eviction complete"
    );
}
```

**Key difference:** No file loop. The `DELETE` SQL statement removes the BLOB data
along with the metadata row. SQLite reclaims the space. We don't need to iterate
through entries to delete files, then do a separate DB delete. It's one operation.

Also note: the DB lock can be held for the entire eviction because there's no file I/O
between the SELECT and DELETE (previously we had to drop the lock to do async file
deletions).

**Net change:** ~10 lines removed.

---

## Struct Changes

**Before:**

```rust
struct ImageCacheInner {
    db: Mutex<Connection>,
    images_dir: PathBuf,        // ← REMOVE this
    total_bytes: AtomicU64,
}
```

**After:**

```rust
struct ImageCacheInner {
    db: Mutex<Connection>,
    total_bytes: AtomicU64,
}
```

Just remove the `images_dir` field. Nothing references it anymore.

---

## Public API (Unchanged)

The public API stays exactly the same. Same methods, same signatures, same return types:

```rust
pub fn open(cache_dir: &Path) -> Result<Self, String>
pub async fn get(&self, url: &str) -> Option<(Vec<u8>, String)>
pub async fn put(&self, url: &str, bytes: &[u8], ext: &str)
```

**No changes needed in any other file:**
- `main.rs` -- calls `ImageCache::open()`, `image_cache.get()`, `image_cache.put()` -- all unchanged
- `fetch_image()` -- calls `image_cache.get()` and `image_cache.put()` -- unchanged
- `download_images_concurrent()` -- calls `fetch_image()` -- unchanged

This is a purely internal refactor.

---

## SQLite BLOB Performance

### Why this is fine for our use case

After resize, images are JPEG thumbnails at max 160x200 pixels. Typical size: **10-100KB**.

SQLite's own documentation recommends BLOBs over external files for objects under 100KB.
Our images are well within that range.

### Specific performance characteristics

| Operation | BLOBs in SQLite | Flat files on disk |
|-----------|----------------|--------------------|
| Read | Single `memcpy` from page cache | Directory traversal → inode lookup → file read |
| Write | Page-level write in WAL | Create file → write → fsync (or tmp+rename) |
| Delete | Remove row from B-tree | `unlink()` per file |
| Atomic | Built-in (transaction) | Manual (tmp+rename pattern) |

For small objects, SQLite reads are typically **faster** than filesystem reads because:
- No directory traversal or inode lookup
- SQLite's page cache keeps hot data in memory
- WAL mode allows concurrent reads during writes

### What about the 20GB limit?

The 20GB cache limit means the DB file could grow large. This is fine because:
- WAL mode keeps read performance stable regardless of DB size
- SQLite B-tree lookups are O(log n) on the primary key -- fast even with millions of rows
- The OS page cache helps with hot data (frequently accessed images)
- Eviction keeps the DB from growing unbounded

### DB file size vs actual data size

When rows are deleted (eviction), SQLite doesn't shrink the file -- it reuses the freed
pages for new data. This means the `.db` file on disk may be larger than the sum of
active data. This is normal and expected. If needed, `VACUUM` can compact it, but it's
rarely necessary because freed pages get reused.

---

## Migration Strategy

The image cache is **ephemeral** -- losing it means a few extra image downloads until it
warms up again. No user data is lost. This makes migration simple.

### Approach: clean break, no data migration

1. The new code creates a new DB file at `<cache_dir>/image_cache.db` (the old one was
   at `<cache_dir>/images/cache.db`)
2. The old `images/` directory with flat files is simply ignored
3. On first startup after upgrade, log:
   ```
   info!("Image cache schema upgraded, old file cache at images/ can be removed")
   ```
4. The cache rebuilds naturally as users make requests -- images get re-downloaded and
   stored in the new SQLite BLOB format

### Why not migrate existing data?

- The cache is a pure performance optimization -- it rebuilds itself through normal usage
- Migration would require reading every file from the old shard directories and inserting
  into SQLite, adding complexity for a one-time operation
- The typical cache warms up fully within a few days of normal usage
- No user-facing impact beyond slightly slower first requests after upgrade

### Optional cleanup (not required)

After confirming the new DB is working, you can optionally delete the old directory:

```rust
// In open(), after successfully creating the new DB:
let old_images_dir = cache_dir.join("images");
if old_images_dir.exists() {
    info!("Old image cache directory found at images/, it can be safely deleted");
    // Or automatically: let _ = std::fs::remove_dir_all(&old_images_dir);
}
```

---

## Test Changes

### Tests to remove

**`test_get_missing_file_fixes_counter`** -- This test verifies that when a cached file
is deleted behind the cache's back, `get()` detects the missing file, cleans up the DB
row, and fixes the byte counter. With BLOBs in SQLite, there are no files to go missing.
This entire class of bug is eliminated. **Delete this test.**

### Tests to modify

All remaining tests stay conceptually the same but need minor adjustments:

**`test_put_and_get`** -- No changes needed. The API is identical.

**`test_get_miss`** -- No changes needed.

**`test_total_bytes_tracking`** -- No changes needed.

**`test_hit_count_increments`** -- No changes needed.

**`test_replace_preserves_hit_count`** -- No changes needed.

**`test_replace_adjusts_total_bytes`** -- No changes needed.

**`test_saturating_sub_no_underflow`** -- No changes needed (utility function unchanged).

**`test_url_hash_deterministic`** -- No changes needed (utility function unchanged).

**`test_url_hash_different_urls`** -- No changes needed (utility function unchanged).

### Tests to add

**`test_blob_round_trip`** -- Verify that raw binary image bytes survive SQLite storage
intact. This is the one new concern: we need to confirm that arbitrary binary data
(including null bytes, high bytes, etc.) is stored and retrieved without corruption.

```rust
#[tokio::test]
async fn test_blob_round_trip() {
    let dir = tempfile::tempdir().unwrap();
    let cache = ImageCache::open(dir.path()).unwrap();

    // Create bytes that exercise edge cases: null bytes, high bytes, all 256 values
    let mut bytes: Vec<u8> = (0..=255).collect();
    // Add some typical JPEG-like data
    bytes.extend_from_slice(&[0xFF, 0xD8, 0xFF, 0xE0]); // JPEG header
    bytes.extend_from_slice(&[0x00, 0x00, 0x00, 0x00]); // null bytes
    bytes.extend_from_slice(&[0xFF, 0xFF, 0xFF, 0xFF]); // high bytes

    cache.put("https://example.com/test.jpg", &bytes, "jpg").await;

    let result = cache.get("https://example.com/test.jpg").await;
    assert!(result.is_some());
    let (got_bytes, got_ext) = result.unwrap();

    // Every single byte must match exactly
    assert_eq!(got_bytes.len(), bytes.len());
    assert_eq!(got_bytes, bytes);
    assert_eq!(got_ext, "jpg");
}
```

**`test_large_blob`** -- Verify that a realistically-sized image (100KB) works fine:

```rust
#[tokio::test]
async fn test_large_blob() {
    let dir = tempfile::tempdir().unwrap();
    let cache = ImageCache::open(dir.path()).unwrap();

    // 100KB of random-ish data (simulating a JPEG thumbnail)
    let bytes: Vec<u8> = (0..100_000).map(|i| (i % 256) as u8).collect();

    cache.put("https://example.com/big.jpg", &bytes, "jpg").await;

    let result = cache.get("https://example.com/big.jpg").await;
    assert!(result.is_some());
    let (got_bytes, _) = result.unwrap();
    assert_eq!(got_bytes.len(), 100_000);
    assert_eq!(got_bytes, bytes);
}
```

---

## Estimated Impact

| Metric | Before | After |
|--------|--------|-------|
| Total lines in `image_cache.rs` | ~530 | ~300 |
| File I/O code | ~200 lines | 0 |
| Error handling paths (file-related) | 8 | 0 |
| Error handling paths (DB-related) | 2 | 2 |
| Race conditions | 2 (shard dir + rename) | 0 |
| Dependencies on `tokio::fs` | Yes (`read`, `write`, `rename`, `remove_file`, `create_dir_all`) | No |
| Struct fields | 3 (`db`, `images_dir`, `total_bytes`) | 2 (`db`, `total_bytes`) |

---

## Files Changed

| File | Change |
|------|--------|
| `src/image_cache.rs` | Rewrite internals (~530 → ~300 lines), same public API |

**No other files change.** The public API is identical, so `main.rs` and all callers
remain untouched.

---

## Checklist

Use this to track implementation progress:

- [ ] Update `ImageCacheInner` struct: remove `images_dir` field
- [ ] Update `open()`: remove directory creation, change DB path, update schema
- [ ] Update `get()`: query BLOB directly, remove file read and self-healing
- [ ] Update `put()`: insert BLOB directly, remove all file I/O
- [ ] Update `evict()`: remove file deletion loop, simplify to batch DELETE only
- [ ] Delete `test_get_missing_file_fixes_counter` test
- [ ] Add `test_blob_round_trip` test
- [ ] Add `test_large_blob` test
- [ ] Run `cargo test` -- all tests should pass
- [ ] Run `cargo clippy` -- no new warnings
- [ ] Manual test: start server, generate a dictionary, verify images appear in popup
- [ ] Optional: add cleanup log for old `images/` directory
