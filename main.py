import fcntl
import hmac
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse


VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass
class DownloadJob:
    job_id: str
    video_id: str
    output_path: Path
    process: subprocess.Popen | None
    status: str


def _environment_settings():
    return {
        "output_dir": Path(os.environ.get(
            "CHAT_OUTPUT_DIR", "/var/lib/chat-downloader-api/chats")),
        "api_token": os.environ.get("CHAT_API_TOKEN", ""),
        "downloader_command": shlex.split(
            os.environ.get("CHAT_DOWNLOADER_COMMAND", "chat_downloader")),
    }


def _parse_boolean(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off", ""):
            return False
    raise HTTPException(
        status_code=422, detail="only_missing must be a boolean")


def _request_values(request, body):
    values = dict(request.query_params)
    if body:
        values.update(body)
    return values


async def _read_body(request):
    if request.method != "POST":
        return {}

    content_type = request.headers.get(
        "content-type", "").split(";", 1)[0].strip()
    if content_type == "application/json":
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=422, detail="JSON body must be an object")
        return body

    if content_type == "application/x-www-form-urlencoded":
        try:
            raw_body = (await request.body()).decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=400, detail="Invalid form body encoding")
        return {
            key: values[-1]
            for key, values in parse_qs(
                    raw_body, keep_blank_values=True).items()
        }

    if not content_type:
        return {}

    raise HTTPException(
        status_code=415,
        detail="Use application/json or application/x-www-form-urlencoded",
    )


def create_app(settings=None):
    config = settings or _environment_settings()
    app = FastAPI(title="Chat Downloader API", version="1.0.0")
    jobs: dict[str, DownloadJob] = {}

    def update_job_status(job):
        if job.status != "running":
            return job.status

        return_code = job.process.poll()
        if return_code is None:
            return job.status

        job.status = (
            "completed"
            if return_code == 0 and job.output_path.is_file()
            else "failed"
        )
        return job.status

    def get_job(job_id):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    async def authorize(request):
        configured_token = config["api_token"]
        if not configured_token:
            raise HTTPException(
                status_code=503,
                detail="API authentication is not configured",
            )

        authorization = request.headers.get("authorization", "")
        scheme, _, supplied_token = authorization.partition(" ")
        if (scheme.lower() != "bearer" or not supplied_token
                or not hmac.compare_digest(supplied_token, configured_token)):
            raise HTTPException(
                status_code=401,
                detail="A valid bearer token is required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.post("/chat-api")
    async def start_chat_download(request: Request, response: Response):
        await authorize(request)
        values = _request_values(request, await _read_body(request))

        video_id = values.get("video_id")
        if (not isinstance(video_id, str)
                or not VIDEO_ID_PATTERN.fullmatch(video_id)):
            raise HTTPException(
                status_code=422,
                detail="video_id must be an 11-character YouTube video ID",
            )
        only_missing = _parse_boolean(values.get("only_missing", False))

        output_dir = Path(config["output_dir"]).expanduser().resolve()
        lock_dir = output_dir / ".locks"
        lock_path = lock_dir / (video_id + ".lock")
        output_path = output_dir / (video_id + ".json")
        job_id = uuid.uuid4().hex
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            lock_dir.mkdir(exist_ok=True)
            with lock_path.open("a+") as lock_file:
                try:
                    fcntl.flock(
                        lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "A chat download for this video is already "
                            "in progress"
                        ),
                    )

                if only_missing and output_path.exists():
                    jobs[job_id] = DownloadJob(
                        job_id=job_id,
                        video_id=video_id,
                        output_path=output_path,
                        process=None,
                        status="completed",
                    )
                    return {
                        "status": "skipped",
                        "job_id": job_id,
                        "video_id": video_id,
                        "status_url": "/chat-api/{}".format(job_id),
                        "result_url": "/chat-api/{}/result".format(job_id),
                    }

                if output_path.exists():
                    output_path = output_dir / (
                        "{}-{}-{}.json".format(
                            video_id, int(time.time()), job_id[:8])
                    )

                command = list(config["downloader_command"]) + [
                    "https://www.youtube.com/watch?v=" + video_id,
                    "--output",
                    str(output_path),
                ]
                child_env = os.environ.copy()
                child_env.pop("CHAT_API_TOKEN", None)
                process = subprocess.Popen(
                    command,
                    cwd=str(output_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    pass_fds=(lock_file.fileno(),),
                    start_new_session=True,
                    env=child_env,
                )
        except OSError:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Could not start the chat downloader; "
                    "check the API service logs"
                ),
            )

        jobs[job_id] = DownloadJob(
            job_id=job_id,
            video_id=video_id,
            output_path=output_path,
            process=process,
            status="running",
        )
        response.status_code = 202
        return {
            "status": "started",
            "job_id": job_id,
            "video_id": video_id,
            "status_url": "/chat-api/{}".format(job_id),
            "result_url": "/chat-api/{}/result".format(job_id),
        }

    @app.get("/chat-api/{job_id}")
    async def get_chat_download_status(job_id: str, request: Request):
        await authorize(request)
        job = get_job(job_id)
        status = update_job_status(job)
        result = {
            "job_id": job.job_id,
            "video_id": job.video_id,
            "status": status,
        }
        if status == "completed":
            result["result_url"] = "/chat-api/{}/result".format(job.job_id)
        return result

    @app.get("/chat-api/{job_id}/result")
    async def get_chat_download_result(job_id: str, request: Request):
        await authorize(request)
        job = get_job(job_id)
        status = update_job_status(job)
        if status == "running":
            raise HTTPException(
                status_code=409, detail="Chat download is not complete")
        if status == "failed":
            raise HTTPException(
                status_code=502, detail="Chat download failed")
        if not job.output_path.is_file():
            raise HTTPException(
                status_code=404, detail="Chat output file not found")
        return FileResponse(job.output_path, media_type="application/json")

    return app


app = create_app()
