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
        self.sheets = {name: self._worksheet(book, name, fields) for name, fields in
                       [("submissions", SUBMISSION_FIELDS), ("corrections", CORRECTION_FIELDS)]}

    @staticmethod
    def _worksheet(book, name, fields):
        import gspread
        try:
            ws = book.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(name, rows=1000, cols=len(fields))
        if not ws.row_values(1):
            ws.append_row(fields)
        return ws

    def append(self, submission, corrections):
        # Corrections first: a submission row only appears once all of its corrections are saved,
        # so a failure partway never leaves a submission that looks complete but isn't.
        if corrections:
            self.sheets["corrections"].append_rows(
                [[str(c.get(f, "")) for f in CORRECTION_FIELDS] for c in corrections], value_input_option="RAW")
        self.sheets["submissions"].append_row(
            [str(submission.get(f, "")) for f in SUBMISSION_FIELDS], value_input_option="RAW")


class LocalStore:
    """For trying the app on your own computer: appends to CSV files next to the app."""

    label = "local CSV files"

    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(exist_ok=True)

    def _append(self, name, fields, rows):
        path = self.folder / f"{name}.csv"
        new = not path.exists()
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
