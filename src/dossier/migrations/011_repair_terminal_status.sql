-- The first `dossier deregistrations` run inferred a terminal status from Form 25, which
-- delists a *security* rather than ending a company. It marked 34 filers in the
-- developer's store and 32 of them had filed a 10-K afterwards: IBM, Procter & Gamble,
-- General Electric, Thermo Fisher. None of them died; each had retired a note or warrant
-- issue.
--
-- `delisted_for_cause` means a total loss in any historical evaluation, so a wrong one is
-- not a cosmetic error: it would write off a live company in every backtest that touched
-- it. This resets any status the filer's own later filings contradict, and every
-- `delisted_for_cause` outright, because nothing in the form index can establish cause.
--
-- Re-running `dossier deregistrations` re-marks what the corrected rule supports.

UPDATE filer
SET status = 'active', status_date = NULL
WHERE status = 'delisted_for_cause'
   OR (
        status = 'deregistered'
        AND EXISTS (
            SELECT 1 FROM filing
            WHERE filing.cik = filer.cik
              AND filing.form_type IN ('10-K', '10-K/A', '20-F', '10-Q')
              AND filing.filed_date > filer.status_date
        )
      );
