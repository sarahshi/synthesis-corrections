"""Report errors in geochemical syntheses (GEOROC, EarthChem/PetDB) so they can be checked and corrected.

Run locally with `streamlit run app.py`. Submissions go to a Google Sheet when one is configured in
.streamlit/secrets.toml (see README.md), and to local_submissions/*.csv otherwise.
"""

import datetime as dt
import io
import re
import uuid

import pandas as pd
import streamlit as st

from storage import get_store

st.set_page_config(page_title="Synthesis corrections", page_icon="🪨", layout="wide")

# %% ----------------------------------------------------------------
# choices

DATABASES = ["GEOROC", "EarthChem / PetDB", "Other"]
ERROR_LOCATIONS = [
    "In the database only (the original publication is correct)",
    "In the original publication (the database copied it faithfully)",
    "Both",
    "Not sure",
]
ERROR_TYPES = [
    "Wrong mineral or phase label",
    "Unit error (e.g., wt% vs. ppm)",
    "Oxide reported as element, or the reverse",
    "Fe reported in the wrong form (FeO, Fe2O3, FeOt)",
    "Values in the wrong column (shifted or swapped)",
    "Not analyzed recorded as 0",
    "Typo or misplaced decimal point",
    "Duplicate analysis",
    "Wrong sample location or coordinates",
    "Wrong citation or sample metadata",
    "Other",
]
SCOPES = {
    "specific": "Specific analyses: I'll list each one below",
    "citation": "Every analysis from this citation (a systematic error, e.g., a whole column in the wrong units)",
}
DETECTION_METHODS = [
    "Compared with the original publication",
    "Stoichiometry or analytical totals",
    "mineralML or another classifier",
    "Compared with other analyses of the same sample",
    "Other",
]

OXIDES = ["SiO2", "TiO2", "Al2O3", "Cr2O3", "FeO", "Fe2O3", "FeOt", "MnO", "MgO", "NiO", "CaO", "Na2O", "K2O",
          "P2O5", "H2O", "CO2", "SO3", "F", "Cl", "Total"]
TRACE = ["Li", "Be", "B", "Sc", "V", "Cr", "Co", "Ni", "Cu", "Zn", "Ga", "Rb", "Sr", "Y", "Zr", "Nb", "Cs", "Ba",
         "La", "Ce", "Pr", "Nd", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu", "Hf", "Ta", "Pb",
         "Th", "U"]
ISOTOPES = ["87Sr/86Sr", "143Nd/144Nd", "176Hf/177Hf", "206Pb/204Pb", "207Pb/204Pb", "208Pb/204Pb"]
METADATA = ["Mineral / phase label", "Sample name", "Latitude", "Longitude", "Rock type", "Analysis method",
            "Other (describe in note)"]
VARIABLES = OXIDES + TRACE + ISOTOPES + METADATA
NUMERIC_VARIABLES = set(OXIDES + TRACE + ISOTOPES + ["Latitude", "Longitude"])
VARIABLE_LOOKUP = {name.lower(): name for name in VARIABLES}  # so an uploaded "k2o" matches "K2O"
UNITS = ["wt%", "ppm", "ppb", "ratio", "degrees", "none"]
# Accepted in place of a number, e.g., when the fix is "this should be blank, not 0"
NOT_A_NUMBER_OK = {"", "na", "n.a.", "nd", "n.d.", "bdl", "blank", "not analyzed", "not analysed"}

COLUMNS = {
    "sample_name": st.column_config.TextColumn("Sample name (as in the database)", width="medium"),
    "database_id": st.column_config.TextColumn("Database sample or analysis ID", width="medium"),
    "variable": st.column_config.SelectboxColumn("Variable", options=VARIABLES, required=True),
    "current_value": st.column_config.TextColumn("Current value"),
    "suggested_value": st.column_config.TextColumn("Suggested value", required=True),
    "unit": st.column_config.SelectboxColumn("Unit", options=UNITS),
    "note": st.column_config.TextColumn("Note", width="large"),
}
EMPTY_TABLE = pd.DataFrame({c: pd.Series(dtype="object") for c in COLUMNS})
TEMPLATE = pd.DataFrame([
    {"sample_name": "ABC-01", "database_id": "", "variable": "Mineral / phase label",
     "current_value": "Clinopyroxene", "suggested_value": "Orthopyroxene", "unit": "none",
     "note": "Wo < 5 and Ca 0.08 apfu"},
    {"sample_name": "ABC-02", "database_id": "", "variable": "K2O", "current_value": "0",
     "suggested_value": "not analyzed", "unit": "wt%", "note": "Blank in Table 2 of the paper"},
])

# %% ----------------------------------------------------------------
# helpers


def clean_table(df):
    """Drop fully empty rows and strip whitespace, so blank lines in the editor don't count."""
    df = df.reindex(columns=list(COLUMNS)).astype("object").where(lambda d: d.notna(), "")
    df = df.apply(lambda col: col.map(lambda v: str(v).strip()))
    df["variable"] = df["variable"].map(lambda v: VARIABLE_LOOKUP.get(v.lower(), v))
    return df[(df != "").any(axis=1)].reset_index(drop=True)


def is_number(value):
    try:
        float(str(value).replace(",", ""))
        return True
    except ValueError:
        return False


def validate(meta, table):
    """Everything wrong with the submission, as messages the submitter can act on."""
    problems = []
    if not meta["databases"]:
        problems.append("Choose which database(s) the error is in.")
    if not meta["citation_author"]:
        problems.append("Add the first author's last name.")
    if not meta["citation_year"]:
        problems.append("Add the publication year.")
    if not (meta["citation_title"] or meta["citation_doi"]):
        problems.append("Add the publication title or DOI, so the citation can be found.")
    if not meta["error_location"]:
        problems.append("Choose where the error is (database, original publication, both, or not sure).")
    if not meta["error_types"]:
        problems.append("Choose at least one type of error.")
    email = meta["submitter_email"]
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        problems.append(f"'{email}' doesn't look like an email address.")
    if table.empty:
        problems.append("Add at least one correction to the table.")
    for i, row in table.iterrows():
        n = i + 1
        if not row["variable"]:
            problems.append(f"Row {n}: choose a variable.")
        elif row["variable"] not in VARIABLES:
            problems.append(f"Row {n}: '{row['variable']}' isn't in the variable list. Choose 'Other (describe "
                            f"in note)' and name it in the note.")
        if not row["suggested_value"]:
            problems.append(f"Row {n}: add the suggested value.")
        if meta["scope"] == "specific" and not (row["sample_name"] or row["database_id"]):
            problems.append(f"Row {n}: add the sample name or database ID (or choose 'Every analysis from "
                            f"this citation' above if the error affects all of them).")
        # A systematic correction is described in words ("divide by 10,000"), so only check numbers per analysis
        if meta["scope"] == "specific" and row["variable"] in NUMERIC_VARIABLES:
            for col, label in [("current_value", "current"), ("suggested_value", "suggested")]:
                v = row[col]
                if v.lower() not in NOT_A_NUMBER_OK and not is_number(v):
                    problems.append(f"Row {n}: the {label} value for {row['variable']} ('{v}') isn't a number.")
    return problems


def reset_form():
    st.session_state.form_version += 1


# %% ----------------------------------------------------------------
# state

st.session_state.setdefault("form_version", 0)
st.session_state.setdefault("n_submitted", 0)
st.session_state.setdefault("last_receipt", None)
v = st.session_state.form_version  # bumped after a submission, which clears every widget below
MAX_PER_SESSION = 20  # a light brake on accidental resubmission and spam

store = get_store()

# %% ----------------------------------------------------------------
# page

st.title("Report an error in a geochemical synthesis")
st.markdown(
    "Found a value in GEOROC or EarthChem/PetDB that doesn't match the original publication, or a "
    "label that can't be right? Report it here. Each report is checked against the original source "
    "before any correction is made. Reports with a citation, the affected analyses, and the corrected "
    "values are the quickest to check."
)

if st.session_state.last_receipt is not None:
    sub_id, receipt = st.session_state.last_receipt
    st.success(f"Thank you! Your report was received. Its ID is **{sub_id}**.")
    st.download_button("Download a copy of your report (CSV)", receipt, file_name=f"report_{sub_id}.csv",
                       mime="text/csv")

st.header("1. The citation")
c1, c2, c3 = st.columns([1.2, 0.6, 1.2])
author = c1.text_input("First author's last name", key=f"author{v}")
year = c2.number_input("Year", min_value=1800, max_value=dt.date.today().year, value=None, step=1,
                       key=f"year{v}")
doi = c3.text_input("DOI", placeholder="10.xxxx/xxxxx", key=f"doi{v}")
title = st.text_input("Title", key=f"title{v}")
c1, c2 = st.columns(2)
databases = c1.multiselect("Database(s) with the error", DATABASES, key=f"db{v}")
db_id = c2.text_input("Citation ID or index code in the database (if known)", key=f"dbid{v}")

st.header("2. The error")
location = st.radio("Where is the error?", ERROR_LOCATIONS, index=None, key=f"loc{v}")
error_types = st.multiselect("Type of error (choose all that apply)", ERROR_TYPES, key=f"types{v}")
scope_label = st.radio("Which analyses are affected?", list(SCOPES.values()), key=f"scope{v}")
scope = next(k for k, label in SCOPES.items() if label == scope_label)

st.header("3. The corrections")
if scope == "citation":
    st.markdown("Describe the systematic change once per variable; leave the sample columns blank. "
                "For example: variable **K2O**, current value **reported in ppm**, suggested value "
                "**divide by 10,000 to get wt%**.")
else:
    st.markdown("One row per corrected value. Add rows with the **+** at the bottom of the table, or upload "
                "a spreadsheet with the same columns as the template.")

u1, u2 = st.columns([3, 1], vertical_alignment="bottom")
upload = u1.file_uploader("Upload corrections (CSV or Excel, optional)", type=["csv", "xlsx", "xls"],
                          key=f"upload{v}")
u2.download_button("Download template CSV", TEMPLATE.to_csv(index=False).encode(),
                   file_name="corrections_template.csv", mime="text/csv")
start = EMPTY_TABLE
if upload is not None:
    try:
        raw = pd.read_csv(upload) if upload.name.lower().endswith(".csv") else pd.read_excel(upload)
        missing = [c for c in ("variable", "suggested_value") if c not in raw.columns]
        if missing:
            st.error(f"The uploaded file is missing the column(s) {', '.join(missing)}. Start from the template.")
        else:
            start = clean_table(raw)
            st.caption(f"Loaded {len(start)} row(s) from {upload.name}. You can edit them below.")
    except Exception as e:
        st.error(f"Couldn't read {upload.name}: {e}")

# A new upload gets a new editor, so the table restarts from the file's rows
editor_key = f"table{v}-{upload.file_id if upload is not None else 'none'}"
table = clean_table(st.data_editor(start, column_config=COLUMNS, num_rows="dynamic", hide_index=True,
                                   width="stretch", key=editor_key))

st.header("4. Evidence")
detection = st.multiselect("How did you find the error?", DETECTION_METHODS, key=f"detect{v}")
evidence = st.text_input("Where are the correct values? (e.g., Table 3, p. 412, of the original paper)",
                         key=f"evidence{v}")
comments = st.text_area(
    "Anything else a curator would need to check this report",
    help="For example: whether the authors published a correction, whether other samples or tables from "
         "this citation are likely affected, or how you worked out the suggested values.",
    key=f"comments{v}",
)

st.header("5. About you (optional)")
st.caption("Only used to follow up on this report and, if you agree, to credit you for it.")
c1, c2, c3 = st.columns(3)
name = c1.text_input("Name", key=f"name{v}")
email = c2.text_input("Email", key=f"email{v}")
affiliation = c3.text_input("Affiliation", key=f"affil{v}")
credit_ok = st.checkbox("You may credit me by name for this correction", key=f"credit{v}")

meta = {
    "submitter_name": name.strip(), "submitter_email": email.strip(), "submitter_affiliation": affiliation.strip(),
    "credit_ok": credit_ok, "databases": "; ".join(databases), "citation_author": author.strip(),
    "citation_year": int(year) if year else "", "citation_title": title.strip(), "citation_doi": doi.strip(),
    "citation_db_id": db_id.strip(), "error_location": location or "", "error_types": "; ".join(error_types),
    "scope": scope, "detection_methods": "; ".join(detection), "evidence_location": evidence.strip(),
    "comments": comments.strip(),
}

st.divider()
if st.button("Submit report", type="primary"):
    problems = validate(meta, table)
    if st.session_state.n_submitted >= MAX_PER_SESSION:
        problems = ["You've sent many reports in this session. Reload the page to send more."]
    if problems:
        st.error("Please fix the following before submitting:\n\n" + "\n".join(f"* {p}" for p in problems))
    else:
        sub_id = f"{dt.date.today():%Y%m%d}-{uuid.uuid4().hex[:6]}"
        submission = {"submission_id": sub_id, "submitted_at": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"), "status": "new", **meta}
        corrections = [{"submission_id": sub_id, "row": i + 1, **row} for i, row in table.iterrows()]
        try:
            store.append(submission, corrections)
        except Exception as e:
            st.error(f"Your report couldn't be saved ({e}). Please try again, or download your table so you "
                     "don't lose it.")
        else:
            buf = io.StringIO()
            pd.DataFrame(corrections).assign(**{k: submission[k] for k in (
                "citation_author", "citation_year", "citation_title", "citation_doi")}).to_csv(buf, index=False)
            st.session_state.last_receipt = (sub_id, buf.getvalue().encode())
            st.session_state.n_submitted += 1
            reset_form()
            st.rerun()

if store.label != "Google Sheet":
    st.caption(f"Development mode: reports are saved to {store.label}, not a Google Sheet.")
