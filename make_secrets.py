"""Write .streamlit/secrets.toml from a service account's JSON key and a Google Sheet key.

    python make_secrets.py path/to/key.json SHEET_KEY
"""

import json
import sys
from pathlib import Path

if len(sys.argv) != 3:
    sys.exit(__doc__)
key_file, sheet_key = sys.argv[1], sys.argv[2]
out = Path(__file__).resolve().parent / ".streamlit" / "secrets.toml"
if out.exists():
    sys.exit(f"{out} already exists. Move or delete it first if you want to replace it.")

account = json.loads(Path(key_file).read_text())
# json.dumps gives a double-quoted string with \n escapes, which TOML reads the same way
lines = [f"sheet_key = {json.dumps(sheet_key)}", "", "[gcp_service_account]"]
lines += [f"{k} = {json.dumps(v)}" for k, v in account.items()]
out.write_text("\n".join(lines) + "\n")
print(f"Wrote {out}\nShare the Sheet with {account['client_email']} as an Editor.")
