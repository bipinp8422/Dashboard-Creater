"""
app.py
------------------------------------------------------------
Upload HTML files -> save them in the same GitHub repository
-> gh-pages branch -> GitHub Pages shareable links.

Streamlit Cloud Secrets required:

GITHUB_TOKEN = "your_new_github_token"

requirements.txt:

streamlit
requests
"""

import base64
from datetime import datetime

import requests
import streamlit as st


# ============================================================
# CONFIGURATION
# ============================================================

GITHUB_REPO = "bipinp8422/Dashboard-Creater"

BASE_BRANCH = "main"

PAGES_BRANCH = "gh-pages"

FOLDER = "reports"

APP_PASSWORD = ""


# ============================================================
# STREAMLIT PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="HTML Dashboard Link Generator",
    page_icon="🔗",
    layout="centered"
)


# ============================================================
# GET GITHUB TOKEN FROM STREAMLIT SECRETS
# ============================================================

try:

    GITHUB_TOKEN = "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"

except Exception:

    st.error("❌ GitHub token is missing.")

    st.info(
        """
        Please add your GitHub token in Streamlit Cloud:

        App → Settings → Secrets

        Add:

        GITHUB_TOKEN = "YOUR_NEW_GITHUB_TOKEN"
        """
    )

    st.stop()


# ============================================================
# OPTIONAL APP PASSWORD
# ============================================================

if APP_PASSWORD:

    password = st.text_input(
        "🔐 Password",
        type="password"
    )

    if password != APP_PASSWORD:

        st.stop()


# ============================================================
# GITHUB CONFIG
# ============================================================

FOLDER = FOLDER.strip("/")

OWNER, REPO_NAME = GITHUB_REPO.split("/", 1)

API = f"https://api.github.com/repos/{GITHUB_REPO}"


HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ============================================================
# HELPER: GITHUB REQUEST ERROR
# ============================================================

def github_error(response):

    try:

        data = response.json()

        message = data.get(
            "message",
            response.text
        )

    except Exception:

        message = response.text

    return (
        f"GitHub returned HTTP {response.status_code}: "
        f"{message}"
    )


# ============================================================
# TEST GITHUB CONNECTION
# ============================================================

def test_github_connection():

    response = requests.get(
        API,
        headers=HEADERS,
        timeout=30
    )

    if response.status_code != 200:

        raise RuntimeError(
            github_error(response)
        )

    return response.json()


# ============================================================
# GET BRANCH
# ============================================================

def get_branch(branch_name):

    response = requests.get(
        f"{API}/git/ref/heads/{branch_name}",
        headers=HEADERS,
        timeout=30
    )

    return response


# ============================================================
# ENSURE GH-PAGES BRANCH EXISTS
# ============================================================

def ensure_branch():

    # --------------------------------------------------------
    # Check gh-pages branch
    # --------------------------------------------------------

    pages_branch = get_branch(PAGES_BRANCH)

    if pages_branch.status_code == 200:

        return True

    if pages_branch.status_code != 404:

        raise RuntimeError(
            f"Unable to check '{PAGES_BRANCH}' branch.\n\n"
            f"{github_error(pages_branch)}"
        )

    # --------------------------------------------------------
    # Get main branch
    # --------------------------------------------------------

    base_branch = get_branch(BASE_BRANCH)

    if base_branch.status_code != 200:

        raise RuntimeError(
            f"Unable to read '{BASE_BRANCH}' branch.\n\n"
            f"{github_error(base_branch)}"
        )

    main_sha = base_branch.json()["object"]["sha"]

    # --------------------------------------------------------
    # Create gh-pages branch
    # --------------------------------------------------------

    response = requests.post(
        f"{API}/git/refs",
        headers=HEADERS,
        timeout=30,
        json={
            "ref": f"refs/heads/{PAGES_BRANCH}",
            "sha": main_sha
        }
    )

    if response.status_code not in (200, 201):

        raise RuntimeError(
            f"Unable to create '{PAGES_BRANCH}' branch.\n\n"
            f"{github_error(response)}"
        )

    return True


# ============================================================
# CREATE REMOTE FILE PATH
# ============================================================

def remote_path(filename):

    if FOLDER:

        return f"{FOLDER}/{filename}"

    return filename


# ============================================================
# CREATE GITHUB PAGES URL
# ============================================================

def page_url(filename):

    path = remote_path(filename)

    return (
        f"https://{OWNER}.github.io/"
        f"{REPO_NAME}/"
        f"{path}"
    )


# ============================================================
# CHECK EXISTING FILE
# ============================================================

def get_existing_file_sha(path):

    url = f"{API}/contents/{path}"

    response = requests.get(
        url,
        headers=HEADERS,
        params={
            "ref": PAGES_BRANCH
        },
        timeout=30
    )

    if response.status_code == 200:

        return response.json()["sha"]

    if response.status_code == 404:

        return None

    raise RuntimeError(
        github_error(response)
    )


# ============================================================
# SAVE HTML FILE TO GITHUB
# ============================================================

def save_to_repo(filename, content_bytes):

    path = remote_path(filename)

    url = f"{API}/contents/{path}"

    # --------------------------------------------------------
    # Get existing file SHA
    # --------------------------------------------------------

    existing_sha = get_existing_file_sha(path)

    # --------------------------------------------------------
    # Encode HTML
    # --------------------------------------------------------

    encoded_content = base64.b64encode(
        content_bytes
    ).decode("utf-8")

    # --------------------------------------------------------
    # GitHub upload payload
    # --------------------------------------------------------

    payload = {
        "message": f"Update dashboard: {filename}",
        "content": encoded_content,
        "branch": PAGES_BRANCH
    }

    # Required when overwriting an existing file
    if existing_sha:

        payload["sha"] = existing_sha

    # --------------------------------------------------------
    # Upload
    # --------------------------------------------------------

    response = requests.put(
        url,
        headers=HEADERS,
        json=payload,
        timeout=60
    )

    if response.status_code not in (200, 201):

        raise RuntimeError(
            github_error(response)
        )

    return page_url(filename)


# ============================================================
# LIST SAVED HTML FILES
# ============================================================

def list_saved():

    if FOLDER:

        endpoint = f"{API}/contents/{FOLDER}"

    else:

        endpoint = f"{API}/contents"

    response = requests.get(
        endpoint,
        headers=HEADERS,
        params={
            "ref": PAGES_BRANCH
        },
        timeout=30
    )

    if response.status_code != 200:

        return []

    try:

        files = response.json()

    except Exception:

        return []

    if not isinstance(files, list):

        return []

    return sorted(
        [
            item["name"]
            for item in files
            if item.get("type") == "file"
            and item.get("name", "").lower().endswith(
                (".html", ".htm")
            )
        ]
    )


# ============================================================
# APPLICATION HEADER
# ============================================================

st.title("🔗 HTML → Shareable Dashboard Link")

st.caption(
    f"Repository: **{GITHUB_REPO}**  \n"
    f"Branch: **{PAGES_BRANCH}**  \n"
    f"Folder: **{FOLDER or '/'}**"
)


# ============================================================
# GITHUB CONNECTION TEST
# ============================================================

try:

    repo_info = test_github_connection()

    st.success(
        f"✅ GitHub connected: "
        f"{repo_info.get('full_name', GITHUB_REPO)}"
    )

except Exception as error:

    st.error("❌ GitHub connection failed.")

    st.code(
        str(error),
        language="text"
    )

    st.warning(
        """
        Check the following:

        1. The GitHub token is valid.
        2. The token has access to this repository.
        3. Contents permission is Read and Write.
        4. The repository name is correct.
        """
    )

    st.stop()


# ============================================================
# SECURITY WARNING
# ============================================================

st.warning(
    "⚠️ GitHub Pages links are public. "
    "Do not upload confidential, personal, financial, "
    "customer, or otherwise restricted information."
)


# ============================================================
# FILE UPLOAD
# ============================================================

files = st.file_uploader(
    "📁 Upload HTML Dashboard File(s)",
    type=["html", "htm"],
    accept_multiple_files=True
)


# ============================================================
# OPTIONS
# ============================================================

keep_names = st.checkbox(
    "Keep original file names",
    value=True,
    help=(
        "If enabled, uploading the same filename again "
        "will update the existing dashboard link."
    )
)


# ============================================================
# UPLOAD BUTTON
# ============================================================

if files:

    st.write(
        f"**{len(files)} file(s) selected**"
    )

    for uploaded_file in files:

        st.write(
            f"📄 {uploaded_file.name}"
        )


if files and st.button(
    "🚀 Save & Generate Links",
    type="primary",
    use_container_width=True
):

    # --------------------------------------------------------
    # Prepare branch
    # --------------------------------------------------------

    try:

        with st.spinner(
            f"Preparing '{PAGES_BRANCH}' branch..."
        ):

            ensure_branch()

    except Exception as error:

        st.error(
            f"❌ Could not prepare branch '{PAGES_BRANCH}'."
        )

        st.code(
            str(error),
            language="text"
        )

        st.stop()


    # --------------------------------------------------------
    # Upload files
    # --------------------------------------------------------

    results = []

    for uploaded_file in files:

        # Replace spaces with underscores
        filename = uploaded_file.name.replace(
            " ",
            "_"
        )

        # Optional unique filename
        if not keep_names:

            timestamp = datetime.now().strftime(
                "%Y%m%d_%H%M%S"
            )

            filename = (
                f"{timestamp}_{filename}"
            )

        try:

            with st.spinner(
                f"Uploading {filename}..."
            ):

                link = save_to_repo(
                    filename,
                    uploaded_file.getvalue()
                )

            results.append(
                (
                    filename,
                    link
                )
            )

        except Exception as error:

            st.error(
                f"❌ Failed: {filename}"
            )

            st.code(
                str(error),
                language="text"
            )


    # --------------------------------------------------------
    # Show results
    # --------------------------------------------------------

    if results:

        st.success(
            "✅ Dashboard file(s) uploaded successfully!"
        )

        st.markdown(
            "### 🔗 Generated Dashboard Links"
        )

        for filename, link in results:

            st.markdown(
                f"**📄 {filename}**"
            )

            st.code(
                link,
                language=None
            )

            st.markdown(
                f"[🌐 Open Dashboard]({link})"
            )

            st.divider()


# ============================================================
# PREVIOUSLY SAVED FILES
# ============================================================

with st.expander(
    "📁 Previously Saved Dashboards"
):

    try:

        saved_files = list_saved()

    except Exception as error:

        st.error(
            f"Unable to load saved files: {error}"
        )

        saved_files = []


    if not saved_files:

        st.info(
            "No HTML dashboards found yet."
        )

    else:

        st.write(
            f"**{len(saved_files)} dashboard(s) found**"
        )

        for filename in saved_files:

            link = page_url(filename)

            st.markdown(
                f"🔗 [{filename}]({link})"
            )
