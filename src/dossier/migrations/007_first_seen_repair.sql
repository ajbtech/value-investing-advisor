-- `first_seen` was being written with the filer's *most recent* filing date. The as-of
-- universe filters on `first_seen <= as_of`, so every filer vanished from every past
-- date: a screen run as of a year ago returned an empty universe, silently.
--
-- Ingest now records the earliest filing it knows of. This repairs what is already
-- stored, from the filings themselves rather than by re-fetching from EDGAR.

UPDATE filer
SET first_seen = (SELECT MIN(filed_date) FROM filing WHERE filing.cik = filer.cik)
WHERE EXISTS (SELECT 1 FROM filing WHERE filing.cik = filer.cik)
  AND (
        first_seen IS NULL
        OR first_seen > (SELECT MIN(filed_date) FROM filing WHERE filing.cik = filer.cik)
      );
