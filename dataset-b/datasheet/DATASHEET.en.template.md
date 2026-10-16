# {{dataset_name}} Datasheet (version {{dataset_version}})

> **AI-generated captions**
>
> The captions were generated automatically by a local vision-language model ({{caption_models}}) and have not all been checked by a person. In a visual check of {{verification_sample_size}} randomly sampled rows, {{major_error_count}} ({{major_error_pct}}%) had major errors and {{minor_error_count}} ({{minor_error_pct}}%) had minor errors. Location (down to the municipality) and capture time are assigned mechanically from each photo's GPS position and capture date. Photos showing people, vehicle license plates or logos are excluded.
>
> Exclusion relies on automated detection and visual review and is not perfect. If we missed something, we remove it on request (see 10). The photos were taken by the photographer and were not generated, composited or edited with generative AI (apart from the phone's standard image processing). Only the captions and the category are produced by AI.

## 1. Overview

| Item | Value |
|---|---|
| Name | {{dataset_name}} |
| Version / release date | {{dataset_version}} ({{release_date}}) |
| Content | Travel photos (screened to exclude people; every photo with a known location is in Japan) with Japanese and English captions |
| Rows | {{row_count}} (one row per image) |
| Images | JPEG. Long edge in this delivery: {{image_long_edge_min_px}}-{{image_long_edge_max_px}} px. Each buyer chooses either images resized to a 2,048 px long edge or original size. EXIF, XMP and GPS metadata are removed (only an sRGB color profile remains) |
| Captions | Japanese short and detailed captions; English short caption (a translation of the Japanese short caption). AI-generated |
| Other fields | Category (9 classes), location (prefecture and municipality), capture year-month and season, generation provenance, human verification result |
| Photographer / copyright holder | {{licensor_name}} |
| Terms | (Summary) Non-exclusive. Machine-learning training and evaluation only. No redistribution. No use of the images as stock or display content (see 6) |
| Contact | {{contact_email}} |

## 2. Contents

### 2.1 Fields of one row (metadata.jsonl)

"Source" means: **packaging** = computed or assigned when the delivery is built; **AI** = output of the local vision-language model; **metadata** = derived mechanically from the original photo's GPS position or capture date (no AI); **rule** = set by a fixed deterministic procedure; **logged** = recorded during generation; **human** = visual judgment by the photographer.

| Field | Type | Meaning | Source |
|---|---|---|---|
| `id` | string | Row ID (e.g. `{{id_prefix}}-000001`). Not derived from the original file name or capture date | packaging |
| `file_name` | string | Image path. Always `images/` + `id` + `.jpg` | packaging |
| `image_info.width` | integer | Width of the delivered image (px) | packaging |
| `image_info.height` | integer | Height of the delivered image (px) | packaging |
| `image_info.sha256` | string | SHA-256 of the delivered image file (64 lowercase hex digits) | packaging |
| `captions.ja_short` | string | Short Japanese caption. One sentence, plain form, 15-60 characters. Main subject and its situation | AI |
| `captions.ja_detailed` | string | Detailed Japanese caption. 3-5 sentences, plain form, 80-300 characters in total. Composition, season and time-of-day cues (only when visible), light, colors, atmosphere | AI |
| `captions.en_short` | string | Short English caption. A translation of `ja_short` with nothing added. One sentence, 5-30 words | AI |
| `category` | string | Category of the main subject (see 2.2) | AI (some by rule) |
| `location` | object or null | Municipality where the photo was taken. null for photos without GPS and for positions outside Japan's administrative areas (abroad, at sea, etc.) | metadata |
| `location.country` | string | Always `"JP"` | rule |
| `location.prefecture` | string | Prefecture name in Japanese (e.g. `長野県`) | metadata |
| `location.prefecture_en` | string | Prefecture name in English (e.g. `Nagano`), from a fixed 47-entry table | rule |
| `location.municipality` | string | Municipality name in Japanese (e.g. `松本市`, `横浜市中区`, `千代田区`). Wards of ordinance-designated cities are included | metadata |
| `location.municipality_code` | string | 5-digit Japanese local government code (without check digit). The first two digits are the prefecture code | metadata |
| `capture` | object or null | When the photo was taken. null when the photo had no capture date | metadata |
| `capture.year_month` | string | Capture year and month (`YYYY-MM`) | metadata |
| `capture.season` | string or null | `spring` (Mar-May), `summer` (Jun-Aug), `autumn` (Sep-Nov), `winter` (Dec-Feb), from the capture month by the Japan Meteorological Agency convention. null when `location` is null (the hemisphere is unknown) | rule |
| `provenance.caption_source` | string | Always `"ai_generated"` | rule |
| `provenance.caption_model` | string | Name and tag of the model that wrote the captions (see 3.2) | logged |
| `provenance.prompt_version` | string | Caption prompt version (e.g. `caption-v1`) | logged |
| `provenance.category_source` | string | `model` (chosen by the model) or `rule` (set or corrected by a rule) | logged |
| `verification.human_verified` | boolean | true if a person checked the row against the image | human |
| `verification.judgment` | string or null | `correct`, `minor_error` or `major_error` (see 4.1). null for unchecked rows | human |
| `verification.error_fields` | array of string | Fields found to contain an error (`ja_short`, `ja_detailed`, `en_short`, `category`). Empty if none | human |

There are no fields for coordinates, addresses, exact capture date or time, original file names or camera information. The formal definition is `schema/record.v1.schema.json` (JSON Schema draft 2020-12).

### 2.2 Category

Exactly one category per image, chosen by the main subject.

| Code | Japanese label | Main subject | Rows | Share |
|---|---|---|---|---|
| `landscape` | 風景 | Natural scenery: mountains, sea, rivers, lakes, fields, sky (wide shots) | {{category_landscape_count}} | {{category_landscape_pct}}% |
| `cityscape` | 街並み | Streets, alleys, townscapes, town night views, several buildings together | {{category_cityscape_count}} | {{category_cityscape_pct}}% |
| `architecture` | 建物 | One dominant building or structure: temple, shrine, castle, bridge, tower, house | {{category_architecture_count}} | {{category_architecture_pct}}% |
| `railway` | 鉄道 | Trains, stations or tracks as the main subject | {{category_railway_count}} | {{category_railway_pct}}% |
| `transport` | 乗り物 | Non-rail vehicles and vessels: boats, ferries, buses, planes, ropeways | {{category_transport_count}} | {{category_transport_pct}}% |
| `food` | 食 | Dishes, ingredients, drinks, sweets | {{category_food_count}} | {{category_food_pct}}% |
| `flora` | 植物・花 | Flowers, trees or autumn leaves as a close or medium subject | {{category_flora_count}} | {{category_flora_pct}}% |
| `interior` | 屋内 | Indoor spaces (inn rooms, shops) where the space itself is the subject | {{category_interior_count}} | {{category_interior_pct}}% |
| `other` | その他 | None of the above | {{category_other_count}} | {{category_other_pct}}% |

Tie-breaks: a prominent train makes it `railway` even in a landscape; food on a table indoors is `food`; a single shrine gate among trees is `architecture`; a wide view of autumn mountains is `landscape` (not `flora`).

Categories are chosen by the model; {{category_rule_count}} rows were set or corrected by a rule (`provenance.category_source` is `"rule"`).

### 2.3 Regions and time

- Prefectures: {{prefecture_count}} (of 47). Municipalities: {{municipality_count}}
- Top 5 prefectures by rows: {{top_prefectures}}. Together {{top5_prefecture_share_pct}}% of all rows
- No location (`location` is null): {{location_null_count}} rows (may include photos taken outside Japan)
- Capture period: {{capture_first_year_month}} to {{capture_last_year_month}}. No capture date (`capture` is null): {{capture_null_count}} rows

| Spring `spring` | Summer `summer` | Autumn `autumn` | Winter `winter` | Unknown (null) |
|---|---|---|---|---|
| {{season_spring_count}} | {{season_summer_count}} | {{season_autumn_count}} | {{season_winter_count}} | {{season_unknown_count}} |

Full counts per prefecture and municipality can be computed from metadata.jsonl.

## 3. How the dataset was made

### 3.1 Photo selection

1. **Capture**: Photos taken by {{licensor_name}} while traveling, mostly with an iPhone.
2. **Screening**: HEIC converted to JPEG, burst duplicates removed, blur and exposure scored to select usable shots.
3. **Rights check**: Faces/people and text (signs, license plates, logos) detected automatically and sorted into "exclude / needs review / OK"; the photographer then reviewed the results visually. Only photos marked OK are used (see 5).
4. **Candidates for this version**: [CHOOSE: keep one option, delete the others and the brackets] All photos marked OK were used. / Photos were drawn at random from those marked OK (seed {{subset_seed}}). / (describe how they were chosen)
5. **Caption generation and hold-outs**: After the checks in 3.2, {{row_count}} rows remained.

| Stage | Photos |
|---|---|
| Entered screening | {{source_photo_count}} |
| After screening | {{selected_photo_count}} |
| Marked OK in the rights check | {{rights_ok_count}} |
| In this version | {{row_count}} |

### 3.2 Caption and category generation

- **Model**: {{caption_models}}, run locally on the photographer's Mac ({{caption_runtime}}). No cloud API was used.
- **Prompt**: {{prompt_versions}}. Settings: {{caption_generation_settings}}.
- **Generation period**: {{caption_generated_start}} to {{caption_generated_end}}
- **Model input**: Only a downscaled copy of the image with orientation applied and metadata removed (long edge {{caption_input_long_edge_px}} px). Location and capture date were not given to the model, to keep proper nouns out of the captions and to avoid guessing seasons that are not visible.
- **Instructions**: Describe only what is visible. No proper nouns (place names, temple/shrine/facility names, shop/brand/company names, railway line or train model names, mountain names). Do not describe people. Mention season or time of day only when visible cues exist (autumn leaves, cherry blossoms, snow, sunset, night view, etc.). Use numbers only for clearly countable things, five or fewer.
- **Mechanical checks**: Output structure (JSON Schema) and rules such as sentence count, length, word count and sentence endings were checked mechanically. {{caption_failure_excluded_count}} photos that still failed after one retry with a different seed are not included. Factual correctness cannot be checked mechanically (see 4).
- **Second check**: The {{vlm_flagged_count}} photos in which the model reported a person, license plate, or logo/brand name were not included, even where the detection may have been a false positive.
- **Regeneration**: Categories with many major errors are regenerated with a changed prompt or model and then re-verified. Categories regenerated in this version: {{regenerated_categories}}

### 3.3 Location and capture time

- **Location**: The original photo's GPS position was matched offline against administrative boundary data to find the prefecture and municipality. No AI was used. Nothing finer than the municipality is included. Boundary data: {{boundary_data_credit}} (version {{boundary_data_version}})
- **Capture time**: Only the year and month were taken from the original photo's capture date (EXIF). The season is derived mechanically from the capture month, not from the image content.
- Neither is covered by the error-rate check (limitations: see 8).

### 3.4 Image processing for delivery

1. Apply the EXIF orientation to the pixels.
2. Convert to sRGB from the embedded color profile (Display P3 on most iPhone photos).
3. Remove EXIF, XMP, IPTC, GPS and comments (only the basic JPEG header and an sRGB color profile remain).
4. Resize to a 2,048 px long edge (not done for the original-size delivery).
5. Save as JPEG and record the file's SHA-256 in `image_info.sha256`.

The original-size delivery is also not the original file: it is converted to JPEG with metadata removed.

## 4. Quality and error rates

### 4.1 Judgment criteria

| Judgment | Criteria |
|---|---|
| Correct (`correct`) | Everything stated is true of the image. Omissions are not errors |
| Minor error (`minor_error`) | Main subject and category are right, but a secondary detail is wrong (a color, a count, the position of a minor element); or the Japanese is awkward but understandable; or `en_short` differs from `ja_short` only in nuance |
| Major error (`major_error`) | Main subject misidentified; something prominent described that is not there; wrong category; a season or time of day asserted that contradicts the image; any proper noun of a place, facility or brand; a person described; `en_short` meaning materially differs from `ja_short`; text so broken it misleads |

A row's judgment is the worst over `ja_short`, `ja_detailed`, `en_short` and `category`. The fields with errors are listed in `verification.error_fields`.

### 4.2 Sampling

- **Population**: The captions shipped in this version ({{row_count}} rows)
- **Method**: Simple random sample without replacement of {{verification_sample_size}} rows (target 300; all rows if there are fewer). Seed {{verification_seed}}
- **Judge**: The photographer, looking at each image. No AI was used for judging. Judging completed: {{verification_completed_date}}
- If captions are regenerated after judging, the sample is redrawn and judged again, so the numbers below describe the captions that ship. The figures below come from sampling round {{verification_rounds}} (earlier rounds are kept on record).

### 4.3 Results

| Judgment | Rows | Share | 95% CI |
|---|---|---|---|
| Correct | {{correct_count}} | {{correct_pct}}% | — |
| Minor error | {{minor_error_count}} | {{minor_error_pct}}% | — |
| Major error | {{major_error_count}} | {{major_error_pct}}% | {{major_error_ci95_low}}-{{major_error_ci95_high}}% |
| Any error (minor + major) | {{any_error_count}} | {{any_error_pct}}% | {{any_error_ci95_low}}-{{any_error_ci95_high}}% |

95% confidence intervals use the Wilson score method. No finite-population correction is applied, so the intervals are wider than necessary (conservative).

| Field with an error | Rows |
|---|---|
| `ja_short` | {{field_error_ja_short}} |
| `ja_detailed` | {{field_error_ja_detailed}} |
| `en_short` | {{field_error_en_short}} |
| `category` | {{field_error_category}} |

One row can have errors in several fields, so the total can exceed the number of rows with any error ({{any_error_count}}).

### 4.4 Notes for use

- Judged rows keep their captions exactly as generated (they were not hand-corrected). Filter with `verification.judgment` (for example, keep only `"correct"` rows, or drop `"major_error"` rows).
- The error rates are estimates from the sample. They do not guarantee that any individual unchecked row is correct.
- Per-category breakdowns can be computed from `category` and `verification` in metadata.jsonl.
- Location and capture time are derived mechanically from metadata and are not part of these error rates.

## 5. Exclusions and privacy

- **Excluded**: Only photos marked OK in the rights check (automated detection plus the photographer's visual review) are used; that check excludes faces and people, vehicle license plates, logos or brand marks as the main subject, and interiors where photography is restricted. In addition, every photo in which the captioning model reported a person, license plate, or logo/brand name was excluded (3.2).
- **Limits**: Neither automated detection nor visual review is perfect. A small person, a readable plate or other text may remain. If you find one, please contact us; after checking, we remove the row (see 10). While checking the sampled rows, {{review_removed_count}} photos showing a person, license plate or logo were found and removed; unchecked rows may contain such photos at a similar rate.
- **Metadata**: EXIF, XMP and GPS metadata are removed from the delivered images (only an sRGB color profile remains).
- **Rounding**: Location is rounded to the municipality and capture time to the year and month. No coordinates, addresses, exact dates or times, original file names or camera information are included.
- **Captions**: The model is instructed not to describe people or use proper nouns. Not every row has been checked (a violation found in the sample counts as a major error).

## 6. Terms of use (summary)

This is a summary, not legal advice. The binding text is the contract or the marketplace license ({{license_reference}}).

- Non-exclusive license. The same data may be licensed to other buyers.
- Use is limited to training, fine-tuning and evaluating machine-learning models.
- No redistribution, resale or sublicensing of the images or captions.
- No publishing or using the images as stock, editorial or display content.
- No attempts to identify individuals or the exact locations shown.
- Copyright in the photos remains with the photographer ({{licensor_name}}).

## 7. Intended uses / unsuitable uses

**Intended uses**

- Training and evaluating Japanese and English image captioning
- Training image-text matching and retrieval
- Scene classification of Japanese landscapes, townscapes, buildings, food and more (`category`)
- Fine-tuning vision-language models for Japanese

**Unsuitable uses**

- Using unchecked rows as ground truth for evaluation (rows where `verification.judgment` is `"correct"` are better suited as ground truth)
- Tasks about people (photos showing people are excluded)
- Recognizing place or facility names (captions are instructed to avoid proper nouns)
- Fine-grained geolocation (location is municipality-level and refers to where the camera was)
- Treating the season or time of day in captions as ground-truth labels (they describe appearance and are verified only in the sample)
- Learning detailed English descriptions (English is a translated short caption only)
- Treating the data as representative of Japan as a whole (see 8)
- Displaying the images (prohibited by the terms; see 6)

## 8. Known limitations and biases

- **Single photographer**: Subjects, composition and time of day reflect one person's habits.
- **Travel photos**: Mostly tourist spots, scenery seen while traveling, and meals. Everyday living scenes are underrepresented.
- **Bias toward empty scenes**: Because photos with people, plates or logos were excluded, crowded places and busy streets are underrepresented. Excluding photos whose captions could not be generated (3.2) may add further bias.
- **Regions**: {{prefecture_count}} of 47 prefectures. The top 5 prefectures account for {{top5_prefecture_share_pct}}% of rows (see 2.3).
- **Seasons and years**: Counts per season are in the table in 2.3. The capture period is {{capture_first_year_month}} to {{capture_last_year_month}}.
- **Equipment**: Mostly iPhone. Expect the look of smartphone processing (HDR merging, noise reduction) and many wide-angle views.
- **AI caption errors**: Captions may describe things that are not there, get colors, counts or positions wrong, or guess a season or time of day that is not visible (see 4.3). Phrasing can be repetitive.
- **English**: `en_short` is a translation of `ja_short`, not independently written English. There is no English detailed caption.
- **Location**: The municipality is where the camera was, not necessarily where the subject is (for example, a distant mountain). Near boundaries, GPS error can put a photo in the neighboring municipality. Boundaries come from boundary data version {{boundary_data_version}}; later municipal mergers and other changes are not reflected.
- **Capture time**: Based on the camera clock. The season comes from the capture month and may not match how the image looks (for example, a snowy scene in March is `spring`).
- **Similar photos**: Burst duplicates were removed, but similar shots from the same place and day remain. When splitting into training and evaluation sets, we recommend grouping by `location.municipality_code` or `capture.year_month`.
- **Other publication**: Some photos are also sold separately by the photographer as stock photos (now or in the future), so the same images may appear in other data on the web. Keep this in mind for evaluation.

## 9. Distribution format

```text
{{package_dir}}/
├── metadata.jsonl                 one row per image ({{row_count}} rows, UTF-8)
├── images/
│   ├── {{id_prefix}}-000001.jpg
│   └── ...
├── schema/
│   └── record.v1.schema.json      format of one metadata.jsonl row (JSON Schema draft 2020-12)
├── DATASHEET.ja.md
└── DATASHEET.en.md
```

- Total size: {{package_size_gb}} GB
- SHA-256 of metadata.jsonl: `{{metadata_sha256}}`
- The SHA-256 of each image file is in that row's `image_info.sha256`.

Loading with Hugging Face `datasets`:

```python
from datasets import load_dataset

root = "{{package_dir}}"  # folder that contains metadata.jsonl
ds = load_dataset("imagefolder", data_dir=root, split="train")  # images are in the image column

# Example: only rows a person checked and judged correct (reads only the verification column, not the images)
checked = ds.filter(lambda v: v["judgment"] == "correct", input_columns="verification")
```

## 10. Contact and takedown requests

- Contact: {{contact_email}}
- **Takedown requests**: If a row shows a person, a license plate, a logo, or has any other rights problem, contact us with its `id`. We reply within {{takedown_response_days}} days. Confirmed rows are removed in a revised version, and buyers are told which `id`s were removed. We ask buyers to delete the corresponding images and captions.
- **Error reports**: Please also report caption, category or location errors with the `id`. They will be reviewed for the next version.

## 11. Revision history

| Version | Date | Changes |
|---|---|---|
| {{dataset_version}} | {{release_date}} | Initial release |

---

The structure of this datasheet follows Datasheets for Datasets (Gebru et al.) and the Hugging Face dataset card.
