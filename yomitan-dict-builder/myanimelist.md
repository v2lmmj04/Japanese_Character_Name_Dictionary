# MyAnimeList Integration Plan (AniList-to-MAL Bridge)

**Difficulty: 4/10**

---

## Critical Discovery: AniList Character IDs = MAL Character IDs

AniList character IDs and MyAnimeList character IDs are the **same**. Verified:

| AniList char ID | Jikan `/characters/{id}` | name_kanji |
|-----------------|--------------------------|------------|
| 35252           | 35252 - Rintarou Okabe   | 岡部 倫太郎 |
| 34470           | 34470 - Kurisu Makise    | 牧瀬 紅莉栖 |
| 1               | 1 - Spike Spiegel        | スパイク・スピーゲル |

This eliminates the need for:
- Fetching Jikan's character list per media (`/anime/{id}/characters`)
- Name-matching between two different character databases
- Dealing with differing character lists between AniList and Jikan
- Using `idMal` on the media level at all (for character enrichment)

**The approach is now dead simple:** keep AniList as the source of truth for everything
(user lists, character lists, roles, name hints), then optionally enrich individual
characters by calling Jikan `/characters/{anilist_char_id}/full` using the same ID.

---

## What Jikan Enrichment Gives Us

AniList character data is often sparse. Jikan's `/characters/{id}/full` adds:

| Field | AniList (current) | Jikan enrichment |
|-------|-------------------|------------------|
| `about`/description | Often null or very short | Rich freeform bio |
| `name_kanji` | `name.native` (already have) | Same data, backup source |
| `nicknames` | `name.alternative` (already have) | Often more complete |
| Height | Not available | Parseable from `about` |
| Weight | Not available | Parseable from `about` |
| Detailed bio text | Rarely present | Almost always present |

The biggest wins are **height**, **weight**, and **much better descriptions**.

---

## Jikan API Constraints

| Constraint | Value |
|------------|-------|
| Rate limit | 3 req/sec, 60 req/min |
| Auth | None required |
| Cache | Responses cached 24h server-side |
| Method | GET only |

---

## The Approach: Post-Fetch Enrichment

**Do NOT replace the AniList character pipeline.** Instead, add an optional enrichment
step that runs after AniList characters are fetched but before dictionary building.

```
AniList fetch (existing, unchanged)
    │
    ▼
CharacterData (main/primary/side/appears)
    │
    ▼
Jikan enrichment pass (NEW, optional)
  For each character with a numeric ID:
    GET /characters/{id}/full
    Merge: about, height, weight, nicknames
    │
    ▼
Download images (existing, unchanged)
    │
    ▼
Build dictionary (existing, unchanged)
```

This means:
- **Zero changes to `anilist_client.rs`** -- the GraphQL queries stay exactly as-is
- **Zero changes to `UserMediaEntry`** -- no `id_mal` field needed
- **Zero changes to the VNDB path** -- completely untouched
- The enrichment is a standalone pass that can be toggled on/off

---

## Implementation Plan

### Step 1: Create `src/jikan_client.rs` (~200-250 lines)

A small, focused client with exactly two responsibilities:

```rust
pub struct JikanClient {
    client: Client,
}

impl JikanClient {
    pub fn with_client(client: Client) -> Self { ... }

    /// Fetch full character details from Jikan.
    /// Returns parsed fields or None if character not found.
    pub async fn fetch_character_full(&self, mal_id: u32)
        -> Result<JikanCharacterData, String> { ... }

    /// Enrich a CharacterData set by fetching Jikan details for each character.
    /// Skips characters whose AniList ID isn't a valid MAL ID.
    /// Respects rate limits (sequential, 350ms between requests).
    pub async fn enrich_characters(&self, char_data: &mut CharacterData)
        -> EnrichmentStats { ... }
}
```

#### 1a. `fetch_character_full()`
- `GET https://api.jikan.moe/v4/characters/{id}/full`
- Parse: `name_kanji`, `about`, `nicknames`, `images`
- Call `parse_about_field()` to extract height, weight, and prose description
- Retry on 429 with exponential backoff (1s base, 30s cap, 5 retries)
- Return structured data, not directly mutate Character

#### 1b. `enrich_characters()`
- Iterate over all characters in CharacterData
- For each, try `fetch_character_full(character.id)`
- Merge results into existing Character:
  - `description`: prefer Jikan `about` (parsed prose) if AniList description is empty
  - `height`: set from `about` if currently None
  - `weight`: set from `about` if currently None
  - `aliases`: union AniList alternatives + Jikan nicknames (deduplicated)
  - `name_original`: keep AniList `name.native` (already good), use Jikan
    `name_kanji` as fallback if AniList is empty
  - **Do NOT overwrite** `first_name_hint`, `last_name_hint`, `role`, `sex`, `age`,
    `blood_type`, `birthday` -- AniList's structured fields are more reliable than
    regex-parsing Jikan's freeform `about`
- **Throttle: strictly sequential, 350ms between requests** (see Performance section
  for why concurrency is NOT safe here)
- Return stats: { enriched: N, skipped: N, failed: N }

#### 1c. `parse_about_field()` helper (~80 lines)
Jikan's `about` field is freeform text with structured lines at the top followed by
prose. Example:
```
Height: 177 cm (5'10")
Weight: 59 kg (130 lbs)
Birthday: December 14
Blood Type: A

Rintarou Okabe is the protagonist of Steins;Gate...
```

This parser needs to:
1. Split into lines and identify key-value pairs (`Key: Value`)
2. Extract height (regex: `Height:\s*(\d+)\s*cm`) and weight (`Weight:\s*(\d+)\s*kg`)
3. Strip all structured key-value lines from the text
4. Strip HTML entities (`&amp;`, `<br>`, etc.) from the remaining prose
5. Return the cleaned prose as the `description`

```rust
struct AboutParsed {
    height_cm: Option<u32>,
    weight_kg: Option<u32>,
    description: Option<String>,  // prose after stripping structured lines + HTML cleanup
}
```

Only extract height and weight as structured fields. For age, birthday, blood type,
gender -- AniList already provides these as structured fields, so parsing them from
freeform text would be a downgrade in reliability. But the prose description extraction
is non-trivial and needs careful handling of HTML entities and varied formatting.

### Step 2: Wire into `main.rs` (~40-50 lines changed)

#### 2a. Add module
```rust
mod jikan_client;
```

#### 2b. Integrate enrichment into `fetch_anilist_cached()`

The enrichment **must** happen inside `fetch_anilist_cached()` on cache miss, between
the AniList fetch and the `cache.put()` call. This is critical because `media_cache`
stores the result at the end of `fetch_anilist_cached()` -- if enrichment happens
*after* that function returns, the cached data will be un-enriched and every subsequent
cache hit would serve degraded data.

Current flow in `fetch_anilist_cached()` (main.rs:1019-1063):
```
cache miss →
  AniList fetch →
  strip image bytes →
  cache.put() →        // ← caches UN-enriched data
  return
```

New flow:
```
cache miss →
  AniList fetch →
  Jikan enrichment →   // NEW: enrich BEFORE caching
  strip image bytes →
  cache.put() →        // ← now caches ENRICHED data
  return
```

```rust
async fn fetch_anilist_cached(
    media_id: i32,
    media_type: &str,
    state: &AppState,
) -> Result<(String, models::CharacterData, bool), String> {
    // ... cache check (unchanged) ...

    // Cache miss — fetch from API.
    let client = AnilistClient::with_client(state.http_client.clone());
    let (mut char_data, media_title) = client.fetch_characters(media_id, media_type).await?;

    let title = /* ... (unchanged) ... */;

    // NEW: Enrich with Jikan data (height, weight, better descriptions).
    let jikan = JikanClient::with_client(state.http_client.clone());
    let stats = jikan.enrich_characters(&mut char_data).await;
    info!(enriched = stats.enriched, skipped = stats.skipped,
          failed = stats.failed, "Jikan enrichment for {}", title);

    // Clear image bytes before caching (unchanged).
    for c in char_data.all_characters_mut() { /* ... */ }

    // Store in cache (unchanged — now stores enriched data).
    // ...

    Ok((title, char_data, false))
}
```

**Trade-off:** Putting enrichment inside `fetch_anilist_cached` means SSE progress
sub-steps ("Enriching 3/20...") can't be sent from there since `progress_tx` isn't
available. This is acceptable -- the SSE already shows per-media progress, and adding
a `progress_tx` parameter to the cached fetch function would pollute its interface.
If sub-step progress is desired later, the enrichment can be extracted back out and
the cache write moved accordingly.

#### 2c. Handle pre-existing un-enriched cache entries

After deploying this feature, the `media_cache` will contain entries from before
enrichment existed. These have 30-day TTL and will refresh naturally. This is
acceptable -- there's no need for cache invalidation. If faster rollout is desired,
a one-time `DELETE FROM media_cache` on deploy is trivial.

### Step 3: Tests (~40-60 lines)

- `parse_about_field()` with various real MAL character bios:
  - Standard format (Height/Weight/Birthday/Blood Type lines + prose)
  - Missing fields (no Height line, etc.)
  - HTML entities in `about` (`&amp;`, `<br>`, `&#039;`)
  - `about` is null or empty string
  - Only prose, no structured lines
- `enrich_characters()` with mock data verifying merge logic
- Verify AniList fields are NOT overwritten by Jikan data
- Edge case: character ID is not a valid MAL ID (returns 404, silently skipped)

### No frontend changes needed

The enrichment is transparent. Users input their AniList username as before and get
richer character data automatically.

---

## Performance Impact: The Real Numbers

For a user with N anime in "Currently Watching", each with ~C characters:

| Step | Requests | Time (uncached) |
|------|----------|-----------------|
| AniList user list | 2 (anime + manga) | ~1s |
| AniList characters (per media) | ceil(C/25) pages | ~0.5s/page |
| **Jikan enrichment (per media)** | **C requests** | **C × 350ms** |
| Image downloads | C (concurrent) | ~2-5s |

Example: 5 anime, avg 20 characters each = 100 Jikan requests = **~35 seconds** added.

**This is slow, but acceptable.** The richer data is worth the wait, and caching means
the cost is only paid once per media (30-day TTL).

### Why concurrency does NOT help here

Jikan's rate limit is **3 req/sec** and **60 req/min**. A 350ms sequential delay gives
~2.85 req/sec, safely under the 3/sec limit. Using a semaphore with 2-3 concurrent
requests at 350ms delay would yield 6-9 req/sec, **immediately triggering 429s**. To
run N concurrent requests safely, the inter-request delay must be N × 333ms, which
gives the same total wall-clock time as sequential. Concurrency provides zero benefit
under a per-second rate limit.

The only way to speed this up would be to reduce the number of requests (e.g., skip
background characters, only enrich main/primary roles). This is a viable optimization
but not for the initial implementation.

### Mitigations for the wait time:

1. **Caching absorbs the cost** -- `media_cache` stores the enriched `CharacterData`.
   Second request for the same media is instant. 30-day TTL means most users never
   hit the slow path more than once per anime.

2. **SSE progress** -- Already implemented at the per-media level. Users see which
   anime is being processed. The Jikan enrichment time is absorbed into the per-media
   step. Sub-step progress ("Enriching 3/20...") can be added later if needed.

3. **Skip background characters** (future optimization) -- Only enrich main + primary
   roles. For a 20-character anime with 3 main + 5 primary, this cuts Jikan calls
   from 20 to 8, reducing enrichment time from ~7s to ~2.8s per media.

---

## Files Changed Summary

| File | Change | Lines |
|------|--------|-------|
| `src/jikan_client.rs` | **New file**: client + parse_about + enrichment | ~200-250 |
| `src/main.rs` | Add `mod jikan_client`, call enrichment inside `fetch_anilist_cached()` | ~15-20 |
| `src/models.rs` | No changes | 0 |
| `src/anilist_client.rs` | No changes | 0 |
| `static/index.html` | No changes | 0 |
| Tests | `parse_about_field`, merge logic | ~40-60 |
| **Total** | | **~255-330 lines** |

---

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| AniList char ID != MAL char ID for some chars | Low | Jikan returns 404, enrichment silently skips |
| Jikan is down | None | Enrichment fails silently, AniList data used as-is |
| Rate limiting (429) from Jikan | Medium | Exponential backoff (1s base, 30s cap, 5 retries) + 350ms delay; sequential only |
| `about` field format varies across characters | Medium | Defensive regex for height/weight only; prose extraction strips known structured lines and HTML entities |
| Enrichment slows down uncached generation | Medium | ~35s for 100 chars; caching (30-day TTL) ensures this is a one-time cost per media |
| Cache contains un-enriched data from before feature deploy | Low | 30-day TTL refreshes naturally; optional one-time `DELETE FROM media_cache` on deploy |
| `parse_about_field` fails to extract height/weight | Low | Fields stay None (same as current behavior); no regression |

---

## Why Difficulty is 4/10

1. **Zero changes to existing data flow** -- AniList stays the source of truth
2. **Zero model changes** -- `UserMediaEntry` and `Character` unchanged
3. **Zero frontend changes** required
4. **One new file** with focused responsibility
5. **~15-20 lines of changes** to existing code (just `main.rs`)
6. **Graceful degradation** -- if anything fails, existing behavior is preserved
7. The char ID equivalence eliminates the hardest part (cross-referencing two databases)

The meaningful complexity is in `parse_about_field()` (handling varied freeform text,
HTML entities, inconsistent formatting) and the rate-limiting/retry logic. Both have
clear patterns to follow in the existing codebase (`vndb_client.rs` retry logic,
`content_builder.rs` text parsing).
