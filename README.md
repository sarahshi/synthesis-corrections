# Synthesis corrections

A Streamlit form for reporting errors in GEOROC and EarthChem/PetDB: the citation, where the error is,
what kind it is, and any number of `variable / current value / suggested value` corrections, typed into
a table or uploaded as a CSV or Excel file.

## Run it locally

```
pip install -r requirements.txt
streamlit run app.py
```

With no secrets configured, reports are saved to `local_submissions/submissions.csv` and
`local_submissions/corrections.csv`.

## What a report produces

Two tables, linked by `submission_id`:

- **submissions**: one row per report (submitter, citation, databases, error location and types, scope,
  evidence, comments, and a `status` column starting at `new` for curators to update).
- **corrections**: one row per corrected value (sample name, database ID, variable, current value,
  suggested value, unit, note).

## Send reports to a Google Sheet

1. Create a Google Sheet. Its key is the long ID in the URL: `docs.google.com/spreadsheets/d/<key>/edit`.
2. In Google Cloud, create a project, enable the **Google Sheets API**, create a **service account**, and
   download a JSON key for it.
3. Share the Sheet with the service account's `client_email` as an **Editor**.
4. Run `python make_secrets.py path/to/key.json <sheet key>` to write `.streamlit/secrets.toml`
   (or copy `.streamlit/secrets.toml.example` and fill it in by hand).
   On Streamlit Community Cloud, paste the same contents into the app's **Settings → Secrets** instead.

The app creates the `submissions` and `corrections` worksheets, with headers, on the first report.

## Deploy

Push this folder to GitHub and deploy `app.py` on [Streamlit Community Cloud](https://share.streamlit.io).
Apps there sleep after 12 hours without visitors; the next visitor clicks a button and waits while it
restarts. Reports are stored in the Sheet, so they are unaffected.
