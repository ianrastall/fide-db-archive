# FIDE database archive

Historical FIDE rating-list data in one compact, lossless `.bin` file per year
from **1967 through 2025**, plus separate provisional monthly files for **2026**.
The data currently covers **standard ratings**. Each file contains the lists
actually present in the source archive, including partial historical lists;
year coverage does not imply every historical list is available.

Clone or download this repository for all data, or download the small CLI bundle
from Releases and obtain individual binaries from the `data` directory.

The initial snapshot contains **283 source lists and 62,312,616 rating-list
rows** in **69 binaries**. The 7,963,119,616 source SQLite bytes become
834,280,620 binary bytes (**89.5% smaller**). The largest file is
`fide-2025.bin` at **76,204,926 bytes**, below the conservative 100 MB cap.

The binaries are ordinary **Apache Parquet files with Zstandard compression**.
The `.bin` suffix is a naming choice. Compression and column encoding provide
the space savings. All 22 original player fields, duplicate rows, nulls, empty
strings, Unicode, source ordering, source metadata, and SQLite schema definitions
are preserved. No player records are deduplicated or inferred between lists.

## Export on Windows

Install Python 3.11 or newer. The batch launcher creates a local virtual
environment and installs the pinned PyArrow dependency on first use (internet
required for setup). Run these commands from this folder:

```bat
fide-archive.bat export data\fide-2025.bin --output exports\fide-2025.sqlite
fide-archive.bat export data\fide-2025.bin --format csv --output exports\fide-2025.csv
fide-archive.bat export data\fide-2025.bin --format jsonl --output exports\fide-2025.jsonl
```

Export just one original list, restoring its original tables and indexes:

```bat
fide-archive.bat export data\fide-2025.bin --source-db fide-standard-2025-12.sqlite --output exports\fide-standard-2025-12.sqlite
```

Exports refuse to overwrite existing files. SQLite reconstruction preserves
logical data and schema, rather than the original SQLite file's physical bytes.
An annual SQLite export has `players`, `rating_lists`, `list_metadata`, and
`archive_metadata` tables. `players` includes `rating_type` and `source_db` to
identify each original list. Restoring a single list produces its original
22-column `players` table and `meta` table.

JSONL uses JSON `null` for missing values and preserves numeric types. CSV has
headers and uses `\N` for SQL NULL. All literal backslashes are doubled, so
an empty field remains an empty string and a literal `\N` becomes `\\N`.
Decode NULL before unescaping backslashes; numeric columns are documented in
[FORMAT.md](FORMAT.md). JSONL or SQLite is easiest for lossless interchange.

## Other operating systems and Python

```sh
python -m venv .venv
# Activate .venv using your platform's normal command, then:
python -m pip install -r requirements.txt
python archive.py export data/fide-2025.bin --output exports/fide-2025.sqlite
python archive.py info data/fide-1969.bin
python archive.py verify data/fide-2025.bin
```

Parquet-aware tools can also read `.bin` files directly, or after renaming a
copy to `.parquet`. For example:

```python
import pyarrow.parquet as pq
table = pq.read_table("data/fide-2025.bin")
```

The CLI reads an entire binary into memory to retain efficient compression
across a whole year. Allow several GB of free RAM for recent annual files;
building multiple years in parallel uses more memory. SQLite exports will be
substantially larger than the binaries.

## Rebuild and update

```bat
fide-archive.bat build --source D:\fide-archive\sqlite --current-year 2026
fide-archive.bat build --year 2026 --current-year 2026
```

Completed years are grouped annually; the current year is grouped monthly.
`--current-year` defaults to the local calendar year. When 2026 is complete,
run `build --year 2026 --current-year 2027` to create `fide-2026.bin`. After
checking it, remove the superseded twelve monthly files and run `build` again
to regenerate the inventory. The builder deliberately does not delete files.
Only months actually supplied as SQLite sources are generated.

Source filenames must be `fide-standard-YYYY[-MM].sqlite` (also accepts `rapid`
and `blitz` if those sources are added). Every matching input must pass schema,
metadata, row-date/count, and SQLite integrity checks. Sources are opened read
only. Active journal/WAL sidecars are rejected. The default source is
`D:\fide-archive\sqlite`; supplemental inputs from the older project are not
automatically substituted for these sources.

Before a binary replaces an existing file, every decoded field is compared
with the source SQLite rows, source SHA-256 hashes are rechecked, and the file
must be smaller than **100,000,000 bytes**, a conservative margin below
GitHub's 100 MiB regular Git limit. Parquet page checksums detect damaged data
pages. Unchanged inputs skip rewriting after verifying the existing binary.
One builder at a time may write to an output directory. `--jobs 1` reduces
memory use; the default is two worker processes.

`data/inventory.json` records file sizes and SHA-256 hashes, source file hashes,
per-list row counts and provenance, and missing completed years. This inventory
describes the available inputs, not an independent audit of historical accuracy.
The same source metadata is embedded inside each binary, so it is self-contained.

Compare any binary against its source SQLite databases:

```bat
fide-archive.bat verify data\fide-2025.bin --source D:\fide-archive\sqlite
```

## Development

```sh
python -m unittest -v
```

Tests cover annual/monthly grouping, exact row and original-schema restoration,
Unicode and CSV null escaping, unchanged builds, source changes, and failure
preservation, plus detection of damaged data pages and unexpected SQLite value
types. See [FORMAT.md](FORMAT.md) for the binary layout.

## Sources

The supplied SQLite archive derives from FIDE rating lists and historical
OlimpBase transcriptions. Original filenames, available source hashes, conversion
details, and other provenance are retained in each list's `meta` values.
Start with `archive.py info` or the inventory to inspect a particular period.
This repository is an independent archive and is not affiliated with FIDE.
No new license is asserted over third-party rating data.
