-- Point-in-time closing prices, used only to turn a share count into a market cap on
-- the as-of date. `close` is the price as traded that day: any split adjustment the
-- source applied after the fact has been undone before the row was written. Read only
-- through dossier.asof, which filters price_date <= as_of.

CREATE TABLE price (
    cik        INTEGER NOT NULL REFERENCES filer(cik),
    ticker     TEXT    NOT NULL,
    price_date TEXT    NOT NULL,
    close      REAL    NOT NULL,
    source     TEXT    NOT NULL,
    fetched_at TEXT    NOT NULL,
    PRIMARY KEY (cik, price_date, source)
);

CREATE INDEX price_as_of ON price (cik, price_date);
