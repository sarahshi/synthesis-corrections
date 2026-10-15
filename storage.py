"""Where submissions go: a Google Sheet when credentials are configured, local CSV files otherwise."""

import csv
from pathlib import Path

import streamlit as st

# One row per submission (citation, error type, submitter) ...
SUBMISSION_FIELDS = [
    "submission_id", "submitted_at", "status",
    "submitter_name", "submitter_email", "submitter_affiliation", "credit_ok",
    "databases", "citation_author", "citation_year", "citation_title", "citation_doi", "citation_db_id",
    "error_location", "error_types", "scope", "detection_methods", "evidence_location", "comments",
    # Added after the first deployment, so they go at the end and existing sheets keep lining up
    "submitter_orcid", "submitter_role",
    "submitted_at_local", "submitter_timezone", "form_started_at", "seconds_to_complete",
    "n_corrections", "corrections_source", "warnings_at_submit", "session_ref", "user_agent", "app_version",
]
# ... and one row per correction, linked by submission_id.
CORRECTION_FIELDS = [
    "submission_id", "row", "sample_name", "database_id", "variable",
    "current_value", "suggested_value", "unit", "note",
]


class SheetStore:
    """Appends to the 'submissions' and 'corrections' worksheets of one Google Sheet."""

    label = "Google Sheet"

    def __init__(self, credentials, sheet_key):
        import gspread  # only needed when a Sheet is configured
        book = gspread.service_account_from_dict(credentials).open_by_key(sheet_key)
        # Each worksheet with its header row: values are written under the column with their name, so
        # curators can add or reorder columns in the Sheet without breaking new rows
        self.sheets = {name: self._worksheet(book, name, fields) for name, fields in
                       [("submissions", SUBMISSION_FIELDS), ("corrections", CORRECTION_FIELDS)]}

    @staticmethod
    def _worksheet(book, name, fields):
        import gspread
        try:
            ws = book.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(name, rows=1000, cols=len(fields))
        header = ws.row_values(1)
        missing = [f for f in fields if f not in header]
        if missing:  # a new sheet, or fields added to the app since: add their columns at the end
            header += missing
            if ws.col_count < len(header):
                ws.add_cols(len(header) - ws.col_count)
            ws.update(range_name="A1", values=[header])
        return ws, header

    def append(self, submission, corrections):
        # Corrections first: a submission row only appears once all of its corrections are saved,
        # so a failure partway never leaves a submission that looks complete but isn't.
        if corrections:
            ws, header = self.sheets["corrections"]
            ws.append_rows([[str(c.get(f, "")) for f in header] for c in corrections], value_input_option="RAW")
        ws, header = self.sheets["submissions"]
        ws.append_row([str(submission.get(f, "")) for f in header], value_input_option="RAW")


class LocalStore:
    """For trying the app on your own computer: appends to CSV files next to the app."""

    label = "local CSV files"

    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(exist_ok=True)

    def _append(self, name, fields, rows):
        path = self.folder / f"{name}.csv"
        new = not path.exists()
        if not new:  # fields added since the file was started: rewrite it with the new header
            with path.open(newline="") as f:
                reader = csv.DictReader(f)
                old_rows, old_fields = list(reader), reader.fieldnames
            if old_fields != fields:
                with path.open("w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(old_rows)
        with path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new:
                writer.writeheader()
            writer.writerows(rows)

    def append(self, submission, corrections):
        self._append("corrections", CORRECTION_FIELDS, corrections)
        self._append("submissions", SUBMISSION_FIELDS, [submission])


@st.cache_resource
def get_store():
    try:
        secrets = dict(st.secrets)
    except Exception:  # no secrets.toml at all
        secrets = {}
    if "gcp_service_account" in secrets and "sheet_key" in secrets:
        return SheetStore(dict(secrets["gcp_service_account"]), secrets["sheet_key"])
    return LocalStore(Path(__file__).resolve().parent / "local_submissions")
