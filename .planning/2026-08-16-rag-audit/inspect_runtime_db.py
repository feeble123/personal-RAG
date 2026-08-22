"""Read-only consistency inspection for the audit's local SQLite snapshot."""

from __future__ import annotations

import pathlib
import sqlite3


db_path = pathlib.Path("backend/data/app.db").resolve()
connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)

tables = [
    row[0]
    for row in connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
]
print("tables", tables)
print(
    "counts",
    {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables},
)
print("doc_status", list(connection.execute("SELECT status, COUNT(*) FROM documents GROUP BY status")))
print(
    "documents",
    list(
        connection.execute(
            "SELECT id, filename, file_type, page_count, chunk_count, chunk_strategy, status "
            "FROM documents ORDER BY id"
        )
    ),
)
print(
    "kb_docs_chunks",
    list(
        connection.execute(
            "SELECT k.id, COUNT(DISTINCT d.id), COUNT(ch.id) "
            "FROM knowledge_bases k "
            "LEFT JOIN documents d ON d.kb_id = k.id "
            "LEFT JOIN chunks ch ON ch.doc_id = d.id GROUP BY k.id"
        )
    ),
)
print(
    "chunk_count_mismatch",
    list(
        connection.execute(
            "SELECT d.id, d.chunk_count, COUNT(ch.id) actual "
            "FROM documents d LEFT JOIN chunks ch ON ch.doc_id = d.id "
            "GROUP BY d.id HAVING d.chunk_count != COUNT(ch.id)"
        )
    ),
)
print(
    "per_document_chunk_stats",
    list(
        connection.execute(
            "SELECT d.id, d.chunk_strategy, COUNT(ch.id), "
            "ROUND(AVG(LENGTH(ch.content)), 1), MIN(LENGTH(ch.content)), MAX(LENGTH(ch.content)), "
            "SUM(CASE WHEN LENGTH(ch.content) < 100 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN LENGTH(ch.content) > 900 THEN 1 ELSE 0 END), "
            "COUNT(DISTINCT ch.page), SUM(CASE WHEN ch.page IS NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN ch.section IS NULL OR ch.section = '' THEN 1 ELSE 0 END) "
            "FROM documents d LEFT JOIN chunks ch ON ch.doc_id = d.id GROUP BY d.id ORDER BY d.id"
        )
    ),
)
print(
    "document_quality",
    list(connection.execute("SELECT id, quality FROM documents ORDER BY id")),
)
print(
    "doc3_page48_chunks",
    list(
        connection.execute(
            "SELECT chunk_index, section, content FROM chunks "
            "WHERE doc_id = 3 AND page = 48 ORDER BY chunk_index"
        )
    ),
)
print(
    "doc3_length_extremes",
    list(
        connection.execute(
            "SELECT chunk_index, page, section, LENGTH(content), SUBSTR(content, 1, 180) "
            "FROM chunks WHERE doc_id = 3 "
            "ORDER BY LENGTH(content) DESC LIMIT 5"
        )
    ),
)
print(
    "duplicate_hash_groups",
    connection.execute(
        "SELECT COUNT(*) FROM ("
        "SELECT content_hash FROM chunks GROUP BY content_hash HAVING COUNT(*) > 1)"
    ).fetchone()[0],
)
print(
    "citation_orphans",
    connection.execute(
        "SELECT COUNT(*) FROM citations c LEFT JOIN chunks ch ON ch.id = c.chunk_id "
        "WHERE c.chunk_id IS NOT NULL AND ch.id IS NULL"
    ).fetchone()[0],
)
print(
    "assistant_quality_flags",
    list(
        connection.execute(
            "SELECT evidence_level, answer_complete, is_complete, COUNT(*) "
            "FROM messages WHERE role = 'assistant' "
            "GROUP BY evidence_level, answer_complete, is_complete "
            "ORDER BY evidence_level, answer_complete, is_complete"
        )
    ),
)
print(
    "feedback_counts",
    list(
        connection.execute(
            "SELECT feedback, COUNT(*) FROM messages WHERE role = 'assistant' GROUP BY feedback"
        )
    ),
)
connection.close()

chroma_path = pathlib.Path("backend/data/.chroma/chroma.sqlite3").resolve()
chroma = sqlite3.connect(chroma_path.as_uri() + "?mode=ro", uri=True)
chroma_tables = [
    row[0]
    for row in chroma.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
]
print("chroma_tables", chroma_tables)
for table in ("collections", "segments", "embeddings", "embedding_metadata"):
    if table in chroma_tables:
        print(
            f"chroma_{table}_count",
            chroma.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0],
        )
if "collections" in chroma_tables:
    print("chroma_collections", list(chroma.execute("SELECT id, name, dimension FROM collections")))
if "segments" in chroma_tables:
    print("chroma_segments", list(chroma.execute("SELECT id, type, collection FROM segments")))
chroma.close()
