-- 004: a MediaAssist fact is stored once, however many exports contain it.
-- (Re-downloaded Excel files differ byte-wise, e.g. embedded creation time, so
-- file-level hashing alone can't catch repeats.)
CREATE UNIQUE INDEX IF NOT EXISTS mediaassist_rows_fact_uniq ON mediaassist_rows (query_type, md5(data::text));
