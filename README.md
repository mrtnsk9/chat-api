# Chat Downloader API

A small API service that starts the installed `chat_downloader` CLI in the
background. It is intended to live in a separate repository from the downloader.
The downloader itself comes from the
[Chat Downloader repository](https://github.com/mrtnsk9/chat-downloader); this
repository provides only the API wrapper.

## Install and run

From the root of this API repository, create a virtual environment and install
the API requirements plus the downloader directly from its GitHub repository:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt \
  'git+https://github.com/mrtnsk9/chat-downloader.git'
```

Set the required API token and start the service:

```sh
export CHAT_API_TOKEN='replace-with-a-long-random-secret'
export CHAT_OUTPUT_DIR="$HOME/chat-downloads"
uvicorn main:app --host 127.0.0.1 --port 8000
```

`CHAT_API_TOKEN` is required. Set `CHAT_OUTPUT_DIR` to the directory where
downloaded chats should be saved. Downloader output is discarded.
`CHAT_DOWNLOADER_COMMAND` can
override the installed executable (for example, `python -m chat_downloader`).

To run the included API tests, install the development requirements in the
same virtual environment with `python -m pip install -r requirements-dev.txt`.

Keep the service behind HTTPS and do not expose it directly to the public
internet. Keep the token in trusted server-side configuration; do not expose it
in browser code.

## Local development with DDEV

With Docker and DDEV installed, run these commands from the repository root:

```sh
ddev start
ddev exec --service api python -m unittest
```

The API is available at `http://chat-api.ddev.site:8000` and
`https://chat-api.ddev.site:8001`. DDEV uses a development-only token
(`local-ddev-token`) and stores downloaded chats in the API container's
temporary directory. Do not use this token or configuration outside local
development.

## Linux server deployment with systemd

The example service file is in `deploy/chat-downloader-api.service`. These
commands assume Debian or Ubuntu, the API installed at
`/opt/chat-downloader-api`, and the default chat directory. Adjust the paths,
service group, and unit file if your server uses different locations.

Install the prerequisites and create a restricted service account:

```sh
sudo apt update
sudo apt install -y git python3 python3-venv
sudo useradd --system --user-group --no-create-home \
  --shell /usr/sbin/nologin chat-api
```

Clone this repository to `/opt/chat-downloader-api`, then install the API and
downloader into its virtual environment:

```sh
sudo git clone https://github.com/mrtnsk9/chat-api.git /opt/chat-downloader-api
sudo python3 -m venv /opt/chat-downloader-api/.venv
sudo /opt/chat-downloader-api/.venv/bin/pip install \
  -r /opt/chat-downloader-api/requirements.txt \
  'git+https://github.com/mrtnsk9/chat-downloader.git'
```

Create the output directory and give the service account access to it:

```sh
sudo mkdir -p /var/lib/chat-downloader-api/chats
sudo chown chat-api:chat-api /var/lib/chat-downloader-api/chats
sudo chmod 750 /var/lib/chat-downloader-api/chats
```

Create `/etc/chat-downloader-api.env` with a generated secret and the
appropriate paths:

```ini
CHAT_API_TOKEN=replace-with-the-output-of-openssl-rand-hex-32
CHAT_OUTPUT_DIR=/var/lib/chat-downloader-api/chats
CHAT_DOWNLOADER_COMMAND=/opt/chat-downloader-api/.venv/bin/chat_downloader
```

If another service needs to read downloaded files, grant it access to the
output directory using a shared group or ACLs appropriate for your host.

Generate a secret with `openssl rand -hex 32`. Keep this file and the token out
of the repository, and secure it:

```sh
sudo chown root:root /etc/chat-downloader-api.env
sudo chmod 600 /etc/chat-downloader-api.env
```

Install and enable the unit:

```sh
sudo install -D -m 644 deploy/chat-downloader-api.service \
  /etc/systemd/system/chat-downloader-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now chat-downloader-api
sudo systemctl status chat-downloader-api
```

View logs with `sudo journalctl -u chat-downloader-api -f`. The unit listens
only on localhost (`127.0.0.1:8000`); use a secured HTTPS reverse proxy if
clients need to connect from another server. The configured output directory
must match the unit's `ReadWritePaths`. Stopping or restarting the service can
interrupt active downloads.

## Request

Only `POST` requests are supported. Send JSON or URL-encoded form fields, with
`Authorization: Bearer <token>`.

```sh
curl -X POST http://127.0.0.1:8000/chat-api \
  -H 'Authorization: Bearer replace-with-a-long-random-secret' \
  -H 'Content-Type: application/json' \
  -d '{"video_id":"jfKfPfyJRdk","only_missing":true}'
```

The request body must include `video_id`; `only_missing` is optional and
defaults to `false`. `only_missing=true` skips the download when
`<video_id>.json` already exists. If that file exists and `only_missing` is
false, a timestamped unique filename is used. Only one active download per
video ID is allowed in the configured output directory; another request for
that video while it is downloading returns HTTP `409`.

Successful starts return HTTP `202` with a `job_id`, `status_url`, and
`result_url`. A skipped request returns HTTP `200` with the same URLs so the
existing chat can also be retrieved.

Poll `status_url` to check for `running`, `completed`, or `failed`. Fetch
`result_url` to receive the chat JSON after completion. The result request
returns HTTP `409` while the download is still running. All status and result
requests require the same bearer token. Downloader stdout and stderr are
discarded.

For example, after receiving a `job_id` from the start request:

```sh
JOB_ID='paste-the-returned-job-id-here'
curl "http://127.0.0.1:8000/chat-api/$JOB_ID" \
  -H 'Authorization: Bearer replace-with-a-long-random-secret'

curl "http://127.0.0.1:8000/chat-api/$JOB_ID/result" \
  -H 'Authorization: Bearer replace-with-a-long-random-secret'
```

Job status is kept in API process memory, not persisted. Restarting the API
loses the status records, so previously started downloads can no longer be
queried through these endpoints. Invalid IDs return `422`; missing or incorrect
authentication returns `401`. Per-video lock files are kept in the output
directory's hidden `.locks` subdirectory; the operating system releases each
lock automatically when its downloader process exits.
