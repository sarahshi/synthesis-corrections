"""Report errors in geochemical syntheses (GEOROC, EarthChem/PetDB) so they can be checked and corrected.

Run locally with `streamlit run app.py`. Submissions go to a Google Sheet when one is configured in
.streamlit/secrets.toml (see README.md), and to local_submissions/*.csv otherwise.
"""

import datetime as dt
import io
import math
import re
import subprocess
import uuid
import zoneinfo
from pathlib import Path

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
SUBMITTER_ROLES = [
    "An author of the original publication",
    "A user of the database",
    "A database curator or editor",
    "Other",
]
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
# So an uploaded "k2o", "FeOT", or "Sr87_Sr86" (as in database exports) matches the list
VARIABLE_LOOKUP = {name.lower(): name for name in VARIABLES} | {
    "feo*": "FeOt", "feotot": "FeOt", "feo(t)": "FeOt", "feototal": "FeOt", "lat": "Latitude",
    "lon": "Longitude", "long": "Longitude", "mineral": "Mineral / phase label", "phase": "Mineral / phase label",
} | {re.sub(r"(\d+)([A-Za-z]+)/(\d+)([A-Za-z]+)", r"\2\1_\4\3", iso).lower(): iso for iso in ISOTOPES}
UNITS = ["wt%", "ppm", "ppb", "ratio", "degrees", "none"]
UNIT_LOOKUP = {u: u for u in UNITS} | {"wt.%": "wt%", "wt %": "wt%", "%": "wt%", "ug/g": "ppm", "µg/g": "ppm",
                                       "μg/g": "ppm", "mg/kg": "ppm", "ng/g": "ppb", "deg": "degrees", "°": "degrees"}
# Units that make sense for each variable; anything else gets a warning, not a block
EXPECTED_UNITS = {**dict.fromkeys(OXIDES + TRACE, frozenset({"wt%", "ppm", "ppb"})),
                  **dict.fromkeys(ISOTOPES, frozenset({"ratio", "none"})), **dict.fromkeys(METADATA, frozenset({"none"})),
                  "Latitude": {"degrees"}, "Longitude": {"degrees"}}
# Accepted in place of a number, e.g., when the fix is "this should be blank, not 0"
NOT_A_NUMBER_OK = {"", "na", "n.a.", "nd", "n.d.", "bdl", "blank", "not analyzed", "not analysed"}
MAX_UPLOAD_MB = 5  # a corrections file is a few KB; anything this big is a data export

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


def header_key(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


# Uploaded headers can be the template's ("suggested_value"), the table's ("Suggested value"), or close to them
HEADER_LOOKUP = {header_key(k): k for k in COLUMNS} | {header_key(c["label"]): k for k, c in COLUMNS.items()} | {
    "sample": "sample_name", "analysis_id": "database_id", "current": "current_value", "old_value": "current_value",
    "suggested": "suggested_value", "corrected_value": "suggested_value", "new_value": "suggested_value",
    "units": "unit", "notes": "note", "comment": "note", "comments": "note",
}


def normalize_variable(v):
    """'k2o', 'K2O (wt%)', and 'FeOT' become 'K2O', 'K2O', and 'FeOt'; anything unrecognized is kept as is."""
    key = v.strip().lower()
    return VARIABLE_LOOKUP.get(key) or VARIABLE_LOOKUP.get(re.sub(r"\s*\(.*\)$", "", key), v)


def clean_table(df):
    """Drop fully empty rows and strip whitespace, so blank lines in the editor don't count."""
    df = df.reindex(columns=list(COLUMNS)).astype("object").where(lambda d: d.notna(), "")
    df = df.apply(lambda col: col.map(lambda v: str(v).strip()))
    df["variable"] = df["variable"].map(normalize_variable)
    df["unit"] = df["unit"].map(lambda u: UNIT_LOOKUP.get(u.lower(), u))
    return df[(df != "").any(axis=1)].reset_index(drop=True)


def parse_number(value):
    """The number in a cell, or None. Accepts '1,234.5' and '<0.01' (below detection), but not '0,5'."""
    s = str(value).strip().lstrip("<").strip()
    if re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+(\.\d*)?", s):  # thousands separators only
        s = s.replace(",", "")
    try:
        x = float(s)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def implausible(variable, unit, x):
    """Why a parsed value can't be right for its variable, or None."""
    if variable == "Latitude" and abs(x) > 90:
        return "a latitude must be between -90 and 90"
    if variable == "Longitude" and abs(x) > 180:
        return "a longitude must be between -180 and 180"
    if variable in ISOTOPES and x <= 0:
        return "an isotope ratio must be positive"
    if variable in OXIDES + TRACE:
        if x < 0:
            return "a concentration can't be negative"
        if unit == "wt%" and x > 100:
            return "that's over 100 wt% (should the unit be ppm?)"
        if unit == "ppm" and x > 1e6:
            return "that's over 1,000,000 ppm"
    return None


def read_upload(upload):
    """The corrections in an uploaded file, or None, plus messages: (table, errors, warnings)."""
    if upload.size > MAX_UPLOAD_MB * 1e6:
        return None, [f"{upload.name} is {upload.size / 1e6:.0f} MB, too large for a corrections file. If it's a "
                      "data export from a database, download the template and copy in only the values you're "
                      "correcting."], []
    warnings = []
    data = upload.getvalue()
    # Read every cell as text, so sample names like 007 keep their zeros and "NA" isn't turned into a blank
    if upload.name.lower().endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:  # Excel on Windows saves CSVs as cp1252
            text = data.decode("cp1252", errors="replace")
        first_line = text.split("\n", 1)[0]
        sep = max([",", ";", "\t"], key=first_line.count)  # Excel in many European locales saves with ';'
        raw = pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)
    else:
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str, keep_default_na=False)
        # The first sheet with the template's columns, or else the first sheet
        sheet = next((name for name, df in sheets.items()
                      if {"variable", "suggested_value"} <= {HEADER_LOOKUP.get(header_key(c)) for c in df.columns}),
                     next(iter(sheets)))
        raw = sheets[sheet]
        if len(sheets) > 1:
            warnings.append(f"Read the sheet '{sheet}' and ignored the other sheet(s) in {upload.name}.")

    headers = [str(c) for c in raw.columns]
    raw.columns = [HEADER_LOOKUP.get(header_key(c), c) for c in headers]
    raw = raw.loc[:, ~raw.columns.duplicated()]  # e.g., both "note" and "notes": keep the first
    known = [c for c in raw.columns if c in COLUMNS]
    missing = [c for c in ("variable", "suggested_value") if c not in known]
    if missing:
        if not known or len(headers) > 2 * len(COLUMNS):
            return None, [f"{upload.name} doesn't look like a corrections file: it has {len(headers)} columns "
                          f"and no '{missing[0]}' column. If it's a data export from a database, download the "
                          "template and copy in one row per value you're correcting."], warnings
        shown = ", ".join(f"'{h}'" for h in headers[:10]) + (" …" if len(headers) > 10 else "")
        return None, [f"{upload.name} is missing the column(s) {', '.join(missing)} (its columns are {shown}). "
                      "Rename them to match the template."], warnings
    extra = [h for h, c in zip(headers, raw.columns) if c not in COLUMNS]
    if extra:
        warnings.append("Ignored column(s) that aren't in the template: " + ", ".join(f"'{h}'" for h in extra[:10])
                        + (" …" if len(extra) > 10 else "") + ".")
    table = clean_table(raw)
    if table.empty:
        warnings.append(f"{upload.name} has the right columns but no rows.")
    return table, [], warnings


def row_checks(table, scope):
    """Problems with the corrections table as (errors, warnings): errors block submission, warnings don't."""
    errors, warnings = [], []
    seen = {}
    for i, row in table.iterrows():
        n, var, unit = i + 1, row["variable"], row["unit"]
        if var and var not in VARIABLES:
            errors.append(f"Row {n}: '{var}' isn't in the variable list. Choose 'Other (describe in note)' and "
                          "name it in the note.")
        if unit and unit not in UNITS:
            warnings.append(f"Row {n}: '{unit}' isn't one of the listed units ({', '.join(UNITS)}).")
        elif unit and var in EXPECTED_UNITS and unit not in EXPECTED_UNITS[var]:
            warnings.append(f"Row {n}: {var} is usually given in {' or '.join(u for u in UNITS if u in EXPECTED_UNITS[var])}, "
                            f"not {unit}.")
        if row["current_value"] and row["current_value"] == row["suggested_value"]:
            warnings.append(f"Row {n}: the suggested value is the same as the current value.")
        key = (row["sample_name"], row["database_id"], var)
        if var and key in seen:
            warnings.append(f"Row {n} corrects the same variable ({var}) for the same sample as row {seen[key]}.")
        seen.setdefault(key, n)
        # A systematic correction is described in words ("divide by 10,000"), so only check numbers per analysis
        if scope != "specific" or var not in NUMERIC_VARIABLES:
            continue
        for col, label in [("current_value", "current"), ("suggested_value", "suggested")]:
            v = row[col]
            if v.lower() in NOT_A_NUMBER_OK:
                continue
            x = parse_number(v)
            if x is None:
                hint = " Use a period for decimals." if re.fullmatch(r"[+-]?\d+,\d+", v) else ""
                errors.append(f"Row {n}: the {label} value for {var} ('{v}') isn't a number.{hint}")
            elif label == "suggested" and (why := implausible(var, unit, x)):
                warnings.append(f"Row {n}: the suggested {var} is {v}, but {why}.")
    if scope == "citation" and (table["sample_name"] != "").any() | (table["database_id"] != "").any():
        warnings.append("Some rows name a sample, but you chose 'Every analysis from this citation'. If only "
                        "some analyses are affected, choose 'Specific analyses' instead.")
    return errors, warnings


def as_list(intro, items, limit=12):
    more = f"\n* …and {len(items) - limit} more" if len(items) > limit else ""
    return intro + "\n\n" + "\n".join(f"* {p}" for p in items[:limit]) + more


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
    orcid = meta["submitter_orcid"]
    if orcid and not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", orcid):
        problems.append(f"'{orcid}' doesn't look like an ORCID iD, which has the form 0000-0002-1825-0097.")
    if table.empty:
        problems.append("Add at least one correction to the table.")
    for i, row in table.iterrows():
        n = i + 1
        if not row["variable"]:
            problems.append(f"Row {n}: choose a variable.")
        if not row["suggested_value"]:
            problems.append(f"Row {n}: add the suggested value.")
        if meta["scope"] == "specific" and not (row["sample_name"] or row["database_id"]):
            problems.append(f"Row {n}: add the sample name or database ID (or choose 'Every analysis from "
                            f"this citation' above if the error affects all of them).")
    return problems + row_checks(table, meta["scope"])[0]


def reset_form():
    st.session_state.form_version += 1
    st.session_state.form_started = dt.datetime.now(dt.timezone.utc)


@st.cache_resource
def app_version():
    """The deployed commit, so a report can be matched to the checks that were in place when it was sent."""
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def local_time(when, timezone):
    """`when` in the submitter's time zone (from their browser), or '' if it isn't known."""
    try:
        return when.astimezone(zoneinfo.ZoneInfo(timezone)).isoformat(timespec="seconds") if timezone else ""
    except Exception:
        return ""


# %% ----------------------------------------------------------------
# state

st.session_state.setdefault("form_version", 0)
st.session_state.setdefault("n_submitted", 0)
st.session_state.setdefault("last_receipt", None)
st.session_state.setdefault("session_ref", uuid.uuid4().hex[:8])  # shared by every report from one visit
st.session_state.setdefault("form_started", dt.datetime.now(dt.timezone.utc))
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
loaded = None
if upload is not None:
    try:
        loaded, upload_errors, upload_warnings = read_upload(upload)
    except Exception as e:
        loaded, upload_errors, upload_warnings = None, [f"Couldn't read {upload.name}: {e}"], []
    for message in upload_errors:
        st.error(message)
    for message in upload_warnings:
        st.warning(message)
    if loaded is not None:
        start = loaded
        st.caption(f"Loaded {len(start)} row(s) from {upload.name}. You can edit them below.")

# A new upload gets a new editor, so the table restarts from the file's rows
editor_key = f"table{v}-{upload.file_id if upload is not None else 'none'}"
table = clean_table(st.data_editor(start, column_config=COLUMNS, num_rows="dynamic", hide_index=True,
                                   width="stretch", key=editor_key))
# Checked as the table changes, so format problems show up before Submit
row_errors, row_warnings = row_checks(table, scope)
if row_errors:
    st.error(as_list("These rows need fixing before you can submit:", row_errors))
if row_warnings:
    st.warning(as_list("Please double-check these rows (you can still submit):", row_warnings))

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
st.caption("Only used to follow up on this report and, if you agree, to credit you for it. With every report "
           "we also record when it was sent, your time zone, and your browser type, to help curators spot "
           "duplicates and spam.")
c1, c2, c3 = st.columns(3)
name = c1.text_input("Name", key=f"name{v}")
email = c2.text_input("Email", key=f"email{v}")
affiliation = c3.text_input("Affiliation", key=f"affil{v}")
c1, c2 = st.columns(2)
orcid = c1.text_input("ORCID iD", placeholder="0000-0000-0000-0000", key=f"orcid{v}")
role = c2.selectbox("Your connection to this data", SUBMITTER_ROLES, index=None, placeholder="Choose one",
                    key=f"role{v}")
credit_ok = st.checkbox("You may credit me by name for this correction", key=f"credit{v}")

meta = {
    "submitter_name": name.strip(), "submitter_email": email.strip(), "submitter_affiliation": affiliation.strip(),
    "submitter_orcid": re.sub(r"^(https?://)?(www\.)?orcid\.org/", "", orcid.strip(), flags=re.I).upper(),
    "submitter_role": role or "",
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
        st.error(as_list("Please fix the following before submitting:", problems, limit=len(problems)))
    else:
        now = dt.datetime.now(dt.timezone.utc)
        sub_id = f"{now:%Y%m%d}-{uuid.uuid4().hex[:6]}"
        started = st.session_state.form_started
        submission = {
            "submission_id": sub_id, "submitted_at": now.isoformat(timespec="seconds"), "status": "new", **meta,
            "submitted_at_local": local_time(now, st.context.timezone),
            "submitter_timezone": st.context.timezone or "",
            "form_started_at": started.isoformat(timespec="seconds"),
            "seconds_to_complete": round((now - started).total_seconds()),
            "n_corrections": len(table),
            "corrections_source": f"uploaded ({upload.name})" if loaded is not None else "typed",
            "warnings_at_submit": " | ".join(row_warnings),
            "session_ref": st.session_state.session_ref,
            "user_agent": st.context.headers.get("User-Agent", "")[:300],
            "app_version": app_version(),
        }
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
