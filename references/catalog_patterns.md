# Catalog Prefix Filename Patterns

This reference documents the filename patterns encountered in DJ promo files and how they map to the standard `Artist - Title (Mix Type) [Label].ext` format.

## Pattern 1: Catalog Prefix (most common for label promos)

Format: `PREFIX###_Artist__Name_-_Title_(Mix_Type).ext`

- `PREFIX` = uppercase label code (2-8 chars)
- `###` = catalog number (1-6 digits)
- `__` (double underscore) = `&` between artists
- `_` (single underscore) = space
- `_-_` = artist/title separator

Examples:
- `ABL316_Time_Geometry_&_Stacey_Jay_-_Leap_of_Faith_(Extended_Mix).wav`
  → Time Geometry & Stacey Jay - Leap of Faith (Extended Mix) [Ablazing].aiff
- `ARMAS3650_Armin_van_Buuren__Maddix_&_Caroline_Roxy_-_Mouth_Go_LaLa_(Extended_Mix).wav`
  → Armin van Buuren, Maddix & Caroline Roxy - Mouth Go LaLa (Extended Mix) [Armada Music].aiff

Note: Some prefixes start with digits: `2RT013`, `2RBS131`. The regex handles this.

## Pattern 2: Numbered Prefix (compilation/EP tracks)

Format: `## - Artist - Title (Mix) [Label].ext`

- `##` = track number (01, 02, etc.)
- Rest follows standard format

Examples:
- `01 - André Wildenhues - Me Aferro a La Esperanza (Spanish Vocal Mix) [Voice Of Our Hearts].mp3`
- `02 - Benya & Ava - Change You (Maratone Extended Remix) [Maralyzed Records].wav`

## Pattern 3: Standard Format (already close to target)

Format: `Artist - Title (Mix Type).ext` or `Artist - Title (Mix Type) [Label].ext`

May just need label added or minor cleanup.

## Pattern 4: Bare/Unknown (needs AI research)

Files that don't match any pattern. Examples:
- `Better off (Extended mix) mastered.mp3` (missing artist)
- `TRIVIX_GECKO_MASTER V2.0.mp3` (non-standard format)
- `Rodrigo Deem, Kanan - Only Now (Extended Mix) NEWFIXFIX master.wav` (has junk words)

## Known Edge Cases

- Some artists genuinely use "and" instead of "&" — preserve as-is
- "pres." (presents) notation for alias projects: `Dark Moon pres. Suncolours`
- "x" or "X" for certain collabs: `MOKX x SaedYm`, `Alex M.O.R.P.H. X TYGR TYGR`
- Parenthetical country codes: `Charlie Sparks (UK)`
- Mark Sherry style nested parens: `Genesis (Where It Begins) (Extended Mix)`
