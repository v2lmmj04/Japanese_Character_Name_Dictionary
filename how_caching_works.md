# How Caching Works

This project uses a multi-tier caching system to avoid redundant API calls and image downloads. All cache logic lives in `disk_cache.rs`, with orchestration in `main.rs`.

> **Note:** As of the current codebase, all disk cache reads/writes and cleanup tasks are commented out with `// TESTING:` annotations. Only the in-memory image cache is active. The design below describes the full intended behavior.

## Cache Tiers Overview

| Cache | Type | TTL | Storage | What it caches |
|---|---|---|---|---|
| Image cache (in-memory) | `moka::Cache` | 24 hours | RAM (200 MB cap, weighted by byte size) | Resized image bytes + file extension, keyed by source URL |
| Image cache (disk) | `DiskImageCache` | 30 days | `<cache_dir>/images/` | Same data as above, persists across restarts |
| ZIP cache (disk) | `DiskDataCache` | 14 days | `<cache_dir>/zips/` | Finished ZIP files for single-media requests |
| API response cache (disk) | `DiskDataCache` | 21 days | `<cache_dir>/api/` | Serialized `CachedMediaCharacters` (title + character data JSON) per media |
| Download token store | `HashMap` in `Arc<Mutex>` | 5 minutes | RAM | Temporary ZIP bytes for the SSE generate-then-download flow |

The cache directory defaults to `./cache` in debug builds and `/var/cache/yomitan` in release, overridable via the `CACHE_DIR` env var.

## How Each Cache Works

### Image Caching (3-tier lookup)

`fetch_and_cache_image()` runs this waterfall for every character portrait:

1. **In-memory (moka)** — instant hit if the image was fetched recently within the same process lifetime.
2. **Disk** — if the in-memory cache misses, check `DiskImageCache`. On hit, promote the entry back into the in-memory cache.
3. **HTTP download** — fetch from the source URL (VNDB/AniList CDN), resize to thumbnail, convert to WebP, then write to both in-memory and disk caches.

Images are downloaded concurrently (`download_images_concurrent`) with a configurable concurrency limit (8 for VNDB, 6 for AniList).

### API Response Caching

Before calling the VNDB or AniList API for a specific media's characters, the system checks `disk_api_cache` using a key like `api:vndb:v17` or `api:anilist:9253:ANIME`. On hit, it deserializes a `CachedMediaCharacters` struct (title + full character data) and skips the API call entirely. Images still need to be fetched separately since they aren't stored in this cache.

This is the main performance multiplier for username-based requests — if two users both have the same anime in their list, the second request reuses the first's cached API data.

### ZIP Caching (single-media only)

Single-media requests (`?source=vndb&id=v17`) produce deterministic output for the same parameters, so the finished ZIP is cached on disk. The cache key is a SHA-256 of `(source, id, spoiler_level, honorifics, media_type)`.

Username-based requests are NOT ZIP-cached because the user's playing list changes over time. They rely on the API and image caches to make regeneration fast.

### Download Token Store

The SSE streaming endpoint (`/api/generate-stream`) generates a ZIP in the background, stores it in an in-memory `HashMap` keyed by a random UUID, and sends the token to the client. The client then fetches the ZIP via `/api/download?token=UUID`. Tokens expire after 5 minutes, with a cleanup task running every 60 seconds.

## Disk Cache Internals (`disk_cache.rs`)

Both `DiskImageCache` and `DiskDataCache` follow the same pattern:

### File Layout

Each cached entry is two files, named by the SHA-256 hex digest of the key (URL for images, composite key for data):

```
<hash>.img  / <hash>.dat   — raw bytes
<hash>.meta                — JSON metadata (key, size, created_at, ttl)
```

### Atomic Writes

All writes go through a tmp-then-rename pattern to prevent corruption from interrupted writes:

```
write to <hash>.img.tmp  →  rename to <hash>.img
write to <hash>.meta.tmp →  rename to <hash>.meta
```

If the rename fails, the tmp file is cleaned up.

### TTL and Expiry

- `DiskImageCache` uses a hardcoded 30-day TTL (`DISK_TTL_SECS`).
- `DiskDataCache` accepts a configurable TTL at construction (14 days for ZIPs, 21 days for API data).
- On `get()`, if the metadata's `created_at + ttl` is in the past, the entry is treated as a miss and the files are deleted in a background `tokio::spawn`.

### Background Cleanup

Each cache can spawn a cleanup task (`spawn_cleanup_task`) that runs every 6 hours. The cleanup:

1. Removes `.tmp` files (leftover from interrupted writes).
2. Iterates `.meta` files — if expired or corrupt, deletes both `.meta` and its companion (`.img` or `.dat`).
3. Removes orphaned data files that have no corresponding `.meta`.

### Hashing

All cache keys are hashed with SHA-256 (`cache_hash()`) to produce a fixed-length, filesystem-safe filename. The hash is deterministic — same input always produces the same 64-character hex string.
