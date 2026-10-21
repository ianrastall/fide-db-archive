# Binary format, version 1

Each `data/fide-YYYY.bin` or `data/fide-YYYY-MM.bin` is a standalone Apache
Parquet file (opening and closing magic bytes `PAR1`), with Zstandard level 19
compression, dictionary encoding, Parquet data-page version 2.0, and page CRCs.
The writer permits 64 MiB dictionaries and data pages and groups a full year
together (up to 64 million rows per row group). This lets repeated names and
other attributes share dictionaries across months. There is no proprietary
binary decoder or outer wrapper.

## Player schema

The first 22 columns match the source `players` table, in order:

```text
list_date, line_no, rank, rank_note, fide_id, name, fed, sex, title,
w_title, o_title, foa_title, title_raw, rating, change, games, k,
birth_year, birth_date, birth_raw, flag, section
```

`line_no`, `rank`, `fide_id`, `rating`, `change`, `games`, `k`, and `birth_year`
are nullable signed 64-bit integers. All remaining original columns are
nullable UTF-8 strings. SQLite NULL and the empty string remain distinct.
Two appended UTF-8 columns, `rating_type` and `source_db`, identify provenance.
The reader checks the full schema and the manifest version before exporting.

Source lists are ordered by source filename; rows within a source retain
SQLite `rowid` order. Original physical rowid values are not archived.
The `line_no` column is retained verbatim. Year-only dates such as `1969` are
preserved without inventing a month. There is no normalization or deduplication.

## Embedded manifest

Parquet file key/value metadata contains a UTF-8 JSON value under `fide.archive`:

```json
{
  "format_version": 1,
  "period": "2025",
  "provisional": false,
  "lists": []
}
```

Each list entry contains:

| Key | Meaning |
| --- | --- |
| `source_db` | Original SQLite filename |
| `rating_type` | `standard`, `rapid`, or `blitz` |
| `list_date` | Original year or year-month string |
| `rows` | Number of source player rows |
| `row_start` | Zero-based offset of the source's contiguous rows |
| `bytes` | Original SQLite file size |
| `sha256` | SHA-256 of the original SQLite file bytes |
| `meta` | Original key/value pairs, including null values |
| `sqlite_schema` | Original non-null `sqlite_master` type, name, and SQL triples |
| `sqlite_user_version` | Original SQLite user version |

The embedded manifest is authoritative for decoding and source reconstruction.
`data/inventory.json` duplicates it and adds binary sizes, binary SHA-256 hashes,
aggregate row counts, source sizes, the configured current year, and missing
completed years. All of a binary's contents can be exported without that file.

SQLite indexes are stored as SQL definitions and recreated at export time;
SQLite pages, free space, and index B-trees are not archived. SHA-256 source
hashes establish input identity; reconstructed SQLite files are logically
equivalent, not byte-identical to their sources.

## Format references

- [Apache Parquet file format](https://parquet.apache.org/docs/file-format/)
- [Apache Arrow Parquet writer options](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetWriter.html)
- [GitHub's regular Git file limit](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)
