"""
app.py - Upload HTML files -> saved in the SAME GitHub repo as this app -> GitHub Pages links.

Files are committed to a separate branch ("gh-pages") so Streamlit Cloud does NOT
restart the app on every upload (it only watches your main branch).

requirements.txt:
    streamlit
    requests
"""
import base64
from datetime import datetime

import requests
import streamlit as st

# =====================  CONFIG - EDIT HERE  =====================
GITHUB_TOKEN = "github_pat_PASTE_YOUR_TOKEN_HERE"
GITHUB_REPO = "bipinp8422/Dashboard-Creater"   # username/repo
BASE_BRANCH = "main"        # branch where app.py is deployed
PAGES_BRANCH = "gh-pages"   # branch where HTML files are saved
FOLDER = "reports"          # sub-folder inside the Pages branch ("" for root)
APP_PASSWORD = "change-me"  # "" to disable the password
# ================================================================

st.set_page_config(page_title="HTML to Link", page_icon="🔗")
st.title("🔗 HTML → Shareable Link")

if APP_PASSWORD and st.text_input("Password", type="password") != APP_PASSWORD:
    st.stop()

if "PASTE_YOUR_TOKEN" in GITHUB_TOKEN:
    st.error("Paste your GitHub token in GITHUB_TOKEN at the top of app.py.")
    st.stop()

FOLDER = FOLDER.strip("/")
OWNER, NAME = GITHUB_REPO.split("/", 1)
API = f"https://api.github.com/repos/{GITHUB_REPO}"
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def ensure_branch():
    """Create the Pages branch from the base branch if it doesn't exist."""
    r = requests.get(f"{API}/git/ref/heads/{PAGES_BRANCH}", headers=HEADERS, timeout=30)
    if r.status_code == 200:
        return
    base = requests.get(f"{API}/git/ref/heads/{BASE_BRANCH}", headers=HEADERS, timeout=30)
    base.raise_for_status()
    sha = base.json()["object"]["sha"]
    c = requests.post(f"{API}/git/refs", headers=HEADERS, timeout=30,
                      json={"ref": f"refs/heads/{PAGES_BRANCH}", "sha": sha})
    c.raise_for_status()


def remote_path(filename):
    return f"{FOLDER}/{filename}" if FOLDER else filename


def page_url(filename):
    return f"https://{OWNER}.github.io/{NAME}/{remote_path(filename)}"


def save_to_repo(filename, content_bytes):
    """Create or overwrite a file in the repo. Returns the Pages URL."""
    path = remote_path(filename)
    url = f"{API}/contents/{path}"

    sha = None
    g = requests.get(url, headers=HEADERS, params={"ref": PAGES_BRANCH}, timeout=30)
    if g.status_code == 200:
        sha = g.json()["sha"]
    elif g.status_code != 404:
        raise RuntimeError(f"GitHub error {g.status_code}: {g.text}")

    payload = {
        "message": f"Update {path}",
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": PAGES_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    p = requests.put(url, headers=HEADERS, json=payload, timeout=60)
    if p.status_code not in (200, 201):
        raise RuntimeError(f"Upload failed {p.status_code}: {p.text}")
    return page_url(filename)


def list_saved():
    endpoint = f"{API}/contents/{FOLDER}" if FOLDER else f"{API}/contents"
    r = requests.get(endpoint, headers=HEADERS, params={"ref": PAGES_BRANCH}, timeout=30)
    if r.status_code != 200:
        return []
    return [f["name"] for f in r.json() if f["name"].lower().endswith((".html", ".htm"))]


# ---------- UI ----------
st.caption(f"Saving to repo **{GITHUB_REPO}**, branch **{PAGES_BRANCH}**, folder **{FOLDER or '/'}**")
st.warning("GitHub Pages links are public to anyone with the URL. "
           "Share only data you are allowed to make public.")

files = st.file_uploader("Upload HTML file(s)", type=["html", "htm"], accept_multiple_files=True)
keep_names = st.checkbox("Keep original file names (same link every time)", value=True)

if files and st.button("Save & generate links", type="primary"):
    try:
        ensure_branch()
    except Exception as e:
        st.error(f"Could not prepare branch '{PAGES_BRANCH}': {e}")
        st.stop()

    results = []
    for f in files:
        name = f.name.replace(" ", "_")
        if not keep_names:
            name = f"{datetime.now():%Y%m%d_%H%M%S}_{name}"
        try:
            with st.spinner(f"Saving {name}..."):
                results.append((name, save_to_repo(name, f.getvalue())))
        except Exception as e:
            st.error(f"{name}: {e}")

    if results:
        st.success("Saved. GitHub Pages can take about a minute to show new or updated content.")
        for name, link in results:
            st.markdown(f"**{name}**")
            st.code(link, language=None)
            st.markdown(f"[Open]({link})")

with st.expander("Previously saved files"):
    saved = list_saved()
    if not saved:
        st.write("Nothing saved yet.")
    for n in saved:
        st.markdown(f"- [{n}]({page_url(n)})")
