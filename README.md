# YTSave - Cloud-ready video downloader

This app is set up to run on Google Cloud Run, so it does not depend on your PC staying on. It uses only temporary container storage and deletes generated files after they are served.

## Local Requirements

- Python 3.8 or higher
- pip
- ffmpeg

## Local Setup

### Step 1 - Install Python dependencies

```bash
pip install -r requirements.txt
```

### Step 2 - Install ffmpeg

Windows: install from `https://ffmpeg.org/download.html` or use `winget install ffmpeg`

Mac:

```bash
brew install ffmpeg
```

Linux:

```bash
sudo apt install ffmpeg
```

### Step 3 - Run the app

```bash
python app.py
```

### Step 4 - Open in browser

Open `http://localhost:8080`

## Google Cloud Run

This repo now includes:

- `Dockerfile` for a production container
- `gunicorn` instead of the Flask development server
- GitHub Actions auto deploy on every push to `main`

### What this changes

- The server does not run from your PC
- New pushes can redeploy automatically
- The app uses only ephemeral container disk, not persistent local machine storage
- `min-instances 1` keeps one Cloud Run instance warm

### GitHub secrets required

- `GCP_PROJECT_ID`
- `GCP_REGION`
- `CLOUD_RUN_SERVICE`
- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_SERVICE_ACCOUNT`

### First-time Google setup

1. Create an Artifact Registry Docker repository named `ytsave`
2. Create the Cloud Run service name and save it in `CLOUD_RUN_SERVICE`
3. Configure GitHub OIDC to Google Cloud with Workload Identity Federation
4. Push to `main`

After that, every push to `main` builds and deploys the service automatically.

## YouTube auth on hosted servers

YouTube bot checks are the main failure mode for public hosted downloaders. `yt-dlp` is still the best maintained option, but a cloud host often needs fresh cookies and sometimes PO-token settings.

Recommended setup for hosted deployments:

- Store cookies in a secret instead of committing `cookies.txt`
- Export YouTube cookies from a private/incognito browser session
- Refresh cookies when YouTube starts rejecting requests again

Supported environment variables:

- `YTSAVE_YOUTUBE_COOKIES_B64`: base64-encoded Netscape `cookies.txt` content
- `YTSAVE_YOUTUBE_COOKIES`: raw Netscape `cookies.txt` content
- `YTSAVE_YT_PLAYER_CLIENT`: optional comma-separated clients like `mweb` or `web`
- `YTSAVE_YT_PO_TOKEN`: optional comma-separated PO tokens like `mweb.gvs+TOKEN_VALUE`
- `YTSAVE_YT_VISITOR_DATA`: optional visitor data value
- `YTSAVE_YT_PLAYER_SKIP`: optional yt-dlp extractor arg value such as `webpage,configs`
- `YTSAVE_YT_TAB_SKIP`: optional yt-dlp extractor arg value such as `webpage`

When both env vars and local files exist, env vars are used first.
