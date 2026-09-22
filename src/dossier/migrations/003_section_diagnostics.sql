-- Why a section was bounded the way it was.
--
-- extraction_confidence says a parse is doubtful. These say why: which heading started
-- the section, and which item closed it. When Item 1A comes out wrong, that is what an
-- operator needs to see — a score alone tells you to distrust the parse but not how to
-- fix the extractor.

ALTER TABLE document_section ADD COLUMN heading TEXT;
ALTER TABLE document_section ADD COLUMN ended_at TEXT;
