from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from threading import Event, Lock
from typing import Callable
from uuid import uuid4

from flask import (
    Blueprint,
    current_app,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from .extensions import db
from .models import MediaFile, Task
from .storage import backend_for

media_bp = Blueprint("media", __name__)
register_heif_opener()

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mediahub")
_VIEWER_TIMEOUT = 90
_CANCELLED_VIEWER_TTL = 120


@dataclass
class ActiveTranscode:
    task_id: str
    viewers: dict[str, float] = field(default_factory=dict)
    persistent: bool = False
    cancel: Event = field(default_factory=Event)
    started: Event = field(default_factory=Event)
    finished: Event = field(default_factory=Event)


class TranscodeCancelled(Exception):
    pass


_active: dict[str, ActiveTranscode] = {}
_viewers: dict[str, ActiveTranscode] = {}
_cancelled_viewers: dict[str, float] = {}
_active_lock = Lock()
_cache_lock = Lock()
_thumbnail_lock = Lock()


def _prune_cancelled_viewers(now: float) -> None:
    for viewer_id, cancelled_at in list(_cancelled_viewers.items()):
        if now - cancelled_at > _CANCELLED_VIEWER_TTL:
            _cancelled_viewers.pop(viewer_id, None)


def _check_transcode_viewers(job: ActiveTranscode) -> None:
    with _active_lock:
        now = time.monotonic()
        for viewer_id, last_seen in list(job.viewers.items()):
            if now - last_seen > _VIEWER_TIMEOUT:
                job.viewers.pop(viewer_id, None)
                if _viewers.get(viewer_id) is job:
                    _viewers.pop(viewer_id, None)
        if not job.viewers and not job.persistent:
            job.cancel.set()
    if job.cancel.is_set():
        raise TranscodeCancelled


def _stop_process(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            if process.poll() is None:
                process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                if process.poll() is None:
                    raise
        process.wait()


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def enforce_cache_limit(protected: set[Path] | None = None) -> None:
    root = Path(current_app.config["CACHE_DIR"])
    protected = {path.resolve() for path in (protected or set())}
    limit = int(float(current_app.config["CACHE_MAX_GB"]) * 1024**3)
    if limit <= 0 or not root.exists():
        return
    with _cache_lock:
        groups: list[tuple[float, int, Path]] = []
        for category in ("originals", "thumbnails", "hls"):
            directory = root / category
            if not directory.exists():
                continue
            for path in directory.iterdir():
                try:
                    groups.append((path.stat().st_mtime, _path_size(path), path))
                except FileNotFoundError:
                    continue
        total = sum(group[1] for group in groups)
        for _, size, path in sorted(groups):
            if total <= limit:
                break
            resolved = path.resolve()
            if any(
                resolved == item or resolved in item.parents or item in resolved.parents
                for item in protected
            ):
                continue
            marker = path / ".working" if path.is_dir() else None
            if marker and marker.exists():
                continue
            try:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
                total -= size
            except FileNotFoundError:
                pass


def _cache_key(media: MediaFile, variant: str = "") -> str:
    value = f"{media.id}:{media.relative_path}:{media.size}:{media.modified}:{variant}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _cached_original(media: MediaFile) -> Path:
    backend = backend_for(media.media_object.source)
    local = backend.local_file(media.relative_path)
    if local:
        return local
    suffix = PurePosixPath(media.name).suffix
    target = (
        Path(current_app.config["CACHE_DIR"])
        / "originals"
        / f"{_cache_key(media, 'original')}{suffix}"
    )
    if not target.exists() or target.stat().st_size != media.size:
        backend.download(media.relative_path, target)
    os.utime(target, None)
    return target


@media_bp.get("/media/<int:file_id>")
def original(file_id: int):
    media = db.get_or_404(MediaFile, file_id)
    path = _cached_original(media)
    return send_file(path, conditional=True, download_name=media.name)


@media_bp.get("/play/<int:file_id>")
def play(file_id: int):
    media = db.get_or_404(MediaFile, file_id)
    if media.kind != "video":
        return "", 404
    siblings = sorted(
        (item for item in media.media_object.files if item.kind == "video"),
        key=lambda item: item.relative_path.casefold(),
    )
    return render_template(
        "player.html",
        media=media,
        item=media.media_object,
        siblings=siblings,
        playback_id=str(uuid4()),
    )


@media_bp.get("/media/<int:file_id>/thumbnail")
def thumbnail(file_id: int):
    media = db.get_or_404(MediaFile, file_id)
    if media.kind == "video":
        return _video_thumbnail(media)
    if media.kind != "image":
        return "", 404
    target = (
        Path(current_app.config["CACHE_DIR"])
        / "thumbnails"
        / f"{_cache_key(media, 'thumbnail')}.jpg"
    )
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(_cached_original(media)) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((640, 640))
            if image.mode not in {"RGB", "L"}:
                background = Image.new("RGB", image.size, "white")
                if "A" in image.getbands():
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image)
                image = background
            image.convert("RGB").save(target, "JPEG", quality=84, optimize=True)
    os.utime(target, None)
    enforce_cache_limit({target})
    return send_file(target, mimetype="image/jpeg", conditional=True)


def _video_thumbnail_source(media: MediaFile) -> Path | None:
    # A thumbnail must not trigger a full FTP download. Previously viewed FTP
    # videos may already have a complete original in the playback cache.
    local = backend_for(media.media_object.source).local_file(media.relative_path)
    if local is not None:
        return local if local.is_file() else None
    suffix = PurePosixPath(media.name).suffix
    cached = (
        Path(current_app.config["CACHE_DIR"])
        / "originals"
        / f"{_cache_key(media, 'original')}{suffix}"
    )
    if cached.is_file() and cached.stat().st_size == media.size:
        return cached
    return None


def _video_thumbnail(media: MediaFile):
    target = (
        Path(current_app.config["CACHE_DIR"])
        / "thumbnails"
        / f"{_cache_key(media, 'video-thumbnail-v1')}.jpg"
    )
    failure = target.with_suffix(".failed")
    if not target.is_file():
        if failure.is_file() and time.time() - failure.stat().st_mtime < 300:
            return "", 404
        ffmpeg = current_app.config["FFMPEG_PATH"]
        if not shutil.which(ffmpeg):
            return "", 404
        if not _thumbnail_lock.acquire(blocking=False):
            return "", 202, {"Retry-After": "2", "Cache-Control": "no-store"}
        try:
            if not target.is_file():
                source = _video_thumbnail_source(media)
                if source is None:
                    return "", 404
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f"{target.stem}.{uuid4().hex}.jpg")
                try:
                    for position in ("10", "0"):
                        command = [
                            ffmpeg,
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-nostdin",
                            "-y",
                            "-threads",
                            "1",
                            "-filter_threads",
                            "1",
                            "-ss",
                            position,
                            "-i",
                            str(source),
                            "-map",
                            "0:v:0",
                            "-frames:v",
                            "1",
                            "-vf",
                            "scale=480:-2",
                            "-an",
                            "-sn",
                            "-dn",
                            "-q:v",
                            "4",
                            "-threads",
                            "1",
                            "-update",
                            "1",
                            "-f",
                            "image2",
                            str(temporary),
                        ]
                        try:
                            subprocess.run(
                                command,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                check=True,
                                timeout=8,
                            )
                        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                            continue
                        if temporary.is_file() and temporary.stat().st_size:
                            os.replace(temporary, target)
                            break
                finally:
                    temporary.unlink(missing_ok=True)
                if target.is_file():
                    failure.unlink(missing_ok=True)
                else:
                    failure.touch()
        finally:
            _thumbnail_lock.release()
    if not target.is_file():
        return "", 404
    os.utime(target, None)
    enforce_cache_limit({target})
    return send_file(target, mimetype="image/jpeg", conditional=True)


def _probe(path: Path, ffprobe: str) -> dict:
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg не найден. Установите FFmpeg и перезапустите MediaHub.") from exc
    return json.loads(result.stdout)


def _media_duration(probe: dict) -> float | None:
    values = [probe.get("format", {}).get("duration")]
    values.extend(
        stream.get("duration")
        for stream in probe.get("streams", [])
        if stream.get("codec_type") == "video"
    )
    for value in values:
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    return None


def _run_ffmpeg_conversion(command: list[str], cancel_check: Callable[[], None] | None) -> None:
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while process.poll() is None:
            if cancel_check is not None:
                cancel_check()
            time.sleep(0.25)
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command)
    finally:
        if process.poll() is None:
            _stop_process(process)


def _convert_subtitles(
    input_path: Path,
    output_dir: Path,
    streams: list[dict],
    ffmpeg: str,
    cancel_check: Callable[[], None] | None = None,
) -> list[dict]:
    tracks: list[dict] = []
    subtitle_index = 0
    for stream in streams:
        if stream.get("codec_type") != "subtitle":
            continue
        codec = stream.get("codec_name", "")
        tags = stream.get("tags", {})
        if codec in {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}:
            continue
        target = output_dir / f"embedded-{subtitle_index}.vtt"
        try:
            _run_ffmpeg_conversion(
                [ffmpeg, "-y", "-i", str(input_path), "-map", f"0:{stream['index']}", str(target)],
                cancel_check,
            )
            tracks.append(
                {
                    "file": target.name,
                    "label": tags.get("title")
                    or tags.get("language")
                    or f"Субтитры {subtitle_index + 1}",
                    "language": tags.get("language", "ru"),
                }
            )
        except subprocess.CalledProcessError:
            pass
        subtitle_index += 1
    return tracks


def _convert_external_subtitles(
    media: MediaFile,
    output_dir: Path,
    ffmpeg: str,
    cancel_check: Callable[[], None] | None = None,
) -> list[dict]:
    tracks: list[dict] = []
    media_path = PurePosixPath(media.relative_path)
    for candidate in media.media_object.files:
        candidate_path = PurePosixPath(candidate.relative_path)
        if candidate.kind != "subtitle" or candidate_path.parent != media_path.parent:
            continue
        if not candidate_path.stem.casefold().startswith(media_path.stem.casefold()):
            continue
        target = output_dir / f"external-{len(tracks)}.vtt"
        try:
            source = _cached_original(candidate)
            _run_ffmpeg_conversion([ffmpeg, "-y", "-i", str(source), str(target)], cancel_check)
            detail = candidate_path.stem[len(media_path.stem) :].strip(" ._-")
            tracks.append(
                {"file": target.name, "label": detail or candidate.name, "language": detail or "ru"}
            )
        except (OSError, subprocess.CalledProcessError):
            continue
    return tracks


def _hls_attribute(value: str) -> str:
    return str(value).replace("\\", "").replace('"', "'").replace("\r", " ").replace("\n", " ")


def _audio_label(tags: dict, index: int) -> str:
    label = tags.get("title") or tags.get("language") or f"Audio {index + 1}"
    return "default" if str(label).strip().casefold() == "und" else label


def _playlist_duration(path: Path, *, completed_only: bool = False) -> float | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if completed_only and "#EXT-X-ENDLIST" not in lines and "#EXT-X-PLAYLIST-TYPE:VOD" not in lines:
        return None
    durations = []
    for line in lines:
        if line.startswith("#EXTINF:"):
            try:
                duration = float(line.partition(":")[2].partition(",")[0])
            except ValueError:
                return None
            if not math.isfinite(duration) or duration <= 0:
                return None
            durations.append(duration)
    return sum(durations) if durations else None


def _completed_playlist_duration(path: Path) -> float | None:
    return _playlist_duration(path, completed_only=True)


def _refresh_cached_result(result_path: Path, result: dict) -> dict:
    replacements = []
    for track in result.get("audio_tracks", []):
        label = track.get("label", "")
        suffix = label[3:]
        is_duplicate = suffix.startswith(" (") and suffix.endswith(")") and suffix[2:-1].isdigit()
        if label[:3].casefold() == "und" and (not suffix or is_duplicate):
            replacement = "default" + suffix
            track["label"] = replacement
            replacements.append((label, replacement))
    changed = bool(replacements)
    if not result.get("duration"):
        duration = _completed_playlist_duration(result_path.parent / "video.m3u8")
        if duration is not None:
            result["duration"] = duration
            changed = True
    if replacements:
        master_path = result_path.parent / "index.m3u8"
        if master_path.exists():
            master = master_path.read_text(encoding="utf-8")
            for old, new in replacements:
                master = master.replace(
                    f'NAME="{_hls_attribute(old)}"', f'NAME="{_hls_attribute(new)}"'
                )
            temporary = master_path.with_suffix(".m3u8.tmp")
            temporary.write_text(master, encoding="utf-8")
            temporary.replace(master_path)
    if changed:
        temporary = result_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        temporary.replace(result_path)
    return result


def _write_master_playlist(output_dir: Path, video: dict, audio_tracks: list[dict]) -> None:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for position, track in enumerate(audio_tracks):
        attributes = [
            "TYPE=AUDIO",
            'GROUP-ID="audio"',
            f'NAME="{_hls_attribute(track["label"])}"',
            f"DEFAULT={'YES' if position == 0 else 'NO'}",
            "AUTOSELECT=YES",
            'CHANNELS="2"',
            f'URI="audio-{position}.m3u8"',
        ]
        if track.get("language"):
            attributes.insert(3, f'LANGUAGE="{_hls_attribute(track["language"])}"')
        lines.append("#EXT-X-MEDIA:" + ",".join(attributes))
    stream_attributes = ["BANDWIDTH=4500000"]
    width, height = video.get("width"), video.get("height")
    if width and height:
        stream_attributes.append(f"RESOLUTION={width}x{height}")
    if audio_tracks:
        stream_attributes.append('AUDIO="audio"')
    lines.extend(["#EXT-X-STREAM-INF:" + ",".join(stream_attributes), "video.m3u8", ""])
    temporary = output_dir / "index.m3u8.tmp"
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(output_dir / "index.m3u8")


def _playlist_has_segment(path: Path) -> bool:
    try:
        return "#EXTINF:" in path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return False


def _hls_is_ready(output_dir: Path, audio_count: int) -> bool:
    if not _playlist_has_segment(output_dir / "video.m3u8"):
        return False
    return all(
        _playlist_has_segment(output_dir / f"audio-{index}.m3u8") for index in range(audio_count)
    )


def _finalize_hls_playlists(output_dir: Path) -> None:
    for playlist in output_dir.glob("*.m3u8"):
        if playlist.name == "index.m3u8":
            continue
        contents = playlist.read_text(encoding="utf-8")
        contents = contents.replace("#EXT-X-PLAYLIST-TYPE:EVENT", "#EXT-X-PLAYLIST-TYPE:VOD")
        temporary = playlist.with_suffix(".m3u8.tmp")
        temporary.write_text(contents, encoding="utf-8")
        temporary.replace(playlist)


def _store_task_result(task: Task, result: dict, status: str, message: str, progress: int) -> None:
    task.status = status
    task.message = message
    task.progress = progress
    task.result_json = json.dumps(result, ensure_ascii=False)
    db.session.commit()


def _task_identity(task: Task) -> dict:
    context = task.context
    return {key: context[key] for key in ("subject", "object_title", "file_id") if key in context}


def _prepare_worker(
    app,
    file_id: int,
    job: ActiveTranscode,
    audio_index: int,
    bitmap_stream: int | None,
    previous: ActiveTranscode | None = None,
) -> None:
    with app.app_context():
        task = db.session.get(Task, job.task_id)
        process = None
        output_dir = None
        try:
            if previous is not None:
                previous.finished.wait()
            job.started.set()
            _check_transcode_viewers(job)
            task.status = "running"
            task.message = "Подготовка исходного файла"
            task.progress = 10
            db.session.commit()
            media = db.session.get(MediaFile, file_id)
            input_path = _cached_original(media)
            _check_transcode_viewers(job)
            variant = (
                f"multi-audio-v3-subtitle-{bitmap_stream if bitmap_stream is not None else 'none'}"
            )
            key = _cache_key(media, variant)
            hls_root = (Path(app.config["CACHE_DIR"]) / "hls").resolve()
            output_dir = (hls_root / key).resolve()
            if output_dir.parent != hls_root:
                raise ValueError("Недопустимый путь видеокеша")
            if output_dir.exists():
                shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            marker = output_dir / ".working"
            marker.touch()
            probe = _probe(input_path, app.config["FFPROBE_PATH"])
            duration = _media_duration(probe)
            if duration is not None:
                task.result_json = json.dumps(
                    {**task.context, "duration": duration}, ensure_ascii=False
                )
                db.session.commit()
            streams = probe.get("streams", [])
            video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
            audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
            bitmap_streams = [
                stream
                for stream in streams
                if stream.get("codec_type") == "subtitle"
                and stream.get("codec_name")
                in {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
            ]
            if audio_index < 0 or (audio_streams and audio_index >= len(audio_streams)):
                raise ValueError("Звуковая дорожка не найдена")
            if bitmap_stream is not None and bitmap_stream not in {
                int(stream["index"]) for stream in bitmap_streams
            }:
                raise ValueError("Графическая дорожка субтитров не найдена")
            copy_video = video.get("codec_name") == "h264" and bitmap_stream is None
            audio_tracks = []
            used_labels: dict[str, int] = {}
            for index, stream in enumerate(audio_streams):
                tags = stream.get("tags", {})
                base_label = _audio_label(tags, index)
                used_labels[base_label] = used_labels.get(base_label, 0) + 1
                label = (
                    base_label
                    if used_labels[base_label] == 1
                    else f"{base_label} ({used_labels[base_label]})"
                )
                audio_tracks.append(
                    {"index": index, "label": label, "language": tags.get("language", "")}
                )

            def cancel_check() -> None:
                _check_transcode_viewers(job)

            tracks = _convert_subtitles(
                input_path, output_dir, streams, app.config["FFMPEG_PATH"], cancel_check
            )
            tracks.extend(
                _convert_external_subtitles(
                    media, output_dir, app.config["FFMPEG_PATH"], cancel_check
                )
            )
            _check_transcode_viewers(job)
            bitmap_tracks = []
            for stream in bitmap_streams:
                tags = stream.get("tags", {})
                bitmap_tracks.append(
                    {
                        "stream": int(stream["index"]),
                        "label": tags.get("title")
                        or tags.get("language")
                        or f"Bitmap subtitles {len(bitmap_tracks) + 1}",
                    }
                )
            result = {
                **task.context,
                "key": key,
                "tracks": tracks,
                "audio_tracks": audio_tracks,
                "audio_index": audio_index,
                "bitmap_tracks": bitmap_tracks,
                "bitmap_stream": bitmap_stream,
                "duration": duration,
            }

            task.message = "Создание видеопотока"
            task.progress = 30
            db.session.commit()
            command = [app.config["FFMPEG_PATH"], "-y", "-i", str(input_path)]
            if bitmap_stream is None:
                command += ["-map", "0:v:0"]
            else:
                command += [
                    "-filter_complex",
                    f"[0:v:0][0:{bitmap_stream}]overlay[v]",
                    "-map",
                    "[v]",
                ]
            command += ["-an", "-c:v", "copy" if copy_video else "libx264"]
            if not copy_video:
                command += ["-preset", "veryfast", "-crf", "20"]
                if bitmap_stream is None:
                    command += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
            command += [
                "-f",
                "hls",
                "-hls_time",
                "6",
                "-hls_playlist_type",
                "event",
                "-hls_segment_filename",
                str(output_dir / "video-%05d.ts"),
                str(output_dir / "video.m3u8"),
            ]
            for index in range(len(audio_streams)):
                command += [
                    "-map",
                    f"0:a:{index}",
                    "-vn",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-f",
                    "hls",
                    "-hls_time",
                    "6",
                    "-hls_playlist_type",
                    "event",
                    "-hls_segment_filename",
                    str(output_dir / f"audio-{index}-%05d.ts"),
                    str(output_dir / f"audio-{index}.m3u8"),
                ]
            log_path = output_dir / "ffmpeg.log"
            _check_transcode_viewers(job)
            with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
                process = subprocess.Popen(
                    command, stdout=subprocess.DEVNULL, stderr=log_file, text=True
                )
                published = False
                while process.poll() is None:
                    _check_transcode_viewers(job)
                    if not published and _hls_is_ready(output_dir, len(audio_streams)):
                        _write_master_playlist(output_dir, video, audio_tracks)
                        (output_dir / "result.json").write_text(
                            json.dumps(result, ensure_ascii=False), encoding="utf-8"
                        )
                        _store_task_result(
                            task, result, "ready", "Можно смотреть; обработка продолжается", 60
                        )
                        published = True
                    time.sleep(0.25)
            _check_transcode_viewers(job)
            if process.returncode != 0:
                error_text = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                raise RuntimeError(error_text or "FFmpeg failed")
            _finalize_hls_playlists(output_dir)
            if not published:
                _write_master_playlist(output_dir, video, audio_tracks)
            (output_dir / "result.json").write_text(
                json.dumps(result, ensure_ascii=False), encoding="utf-8"
            )
            _store_task_result(task, result, "done", "Видео обработано", 100)
            marker.unlink(missing_ok=True)
            enforce_cache_limit({output_dir, input_path})
        except TranscodeCancelled:
            _stop_process(process)
            db.session.rollback()
            task = db.session.get(Task, job.task_id)
            task.status = "cancelled"
            task.message = "Подготовка отменена"
            task.result_json = json.dumps(_task_identity(task), ensure_ascii=False)
            db.session.commit()
            if output_dir is not None:
                shutil.rmtree(output_dir, ignore_errors=True)
        except Exception as exc:
            _stop_process(process)
            db.session.rollback()
            task = db.session.get(Task, job.task_id)
            task.status = "error"
            task.message = str(exc)
            task.result_json = json.dumps(_task_identity(task), ensure_ascii=False)
            db.session.commit()
            if output_dir is not None:
                try:
                    (output_dir / "result.json").unlink(missing_ok=True)
                    (output_dir / "index.m3u8").unlink(missing_ok=True)
                except OSError:
                    pass
        finally:
            try:
                marker.unlink(missing_ok=True)
            except (NameError, OSError):
                pass
            with _active_lock:
                active_key = f"{file_id}:{bitmap_stream}"
                if _active.get(active_key) is job:
                    _active.pop(active_key, None)
                for viewer_id in job.viewers:
                    if _viewers.get(viewer_id) is job:
                        _viewers.pop(viewer_id, None)
                job.finished.set()


@media_bp.post("/api/media/<int:file_id>/prepare")
def prepare(file_id: int):
    media = db.get_or_404(MediaFile, file_id)
    if media.kind != "video":
        return jsonify(error="Это не видеофайл"), 400
    if not shutil.which(current_app.config["FFMPEG_PATH"]) or not shutil.which(
        current_app.config["FFPROBE_PATH"]
    ):
        return jsonify(error="FFmpeg не найден. Завершите установку и перезапустите MediaHub."), 503
    requested = request.get_json(silent=True) or {}
    viewer_id = requested.get("viewer_id")
    if viewer_id is not None and (not isinstance(viewer_id, str) or not 1 <= len(viewer_id) <= 80):
        return jsonify(error="Некорректный идентификатор просмотра"), 400
    try:
        audio_index = int(requested.get("audio_index", 0))
        raw_bitmap = requested.get("bitmap_stream")
        bitmap_stream = int(raw_bitmap) if raw_bitmap not in {None, "", "none"} else None
    except (TypeError, ValueError):
        return jsonify(error="Некорректный выбор дорожки"), 400
    variant = f"multi-audio-v3-subtitle-{bitmap_stream if bitmap_stream is not None else 'none'}"
    key = _cache_key(media, variant)
    result_path = Path(current_app.config["CACHE_DIR"]) / "hls" / key / "result.json"
    with _active_lock:
        now = time.monotonic()
        _prune_cancelled_viewers(now)
        if viewer_id in _cancelled_viewers:
            return jsonify(status="cancelled", error="Подготовка отменена"), 409
        active_key = f"{file_id}:{bitmap_stream}"
        previous = _active.get(active_key)
        job = previous if previous is not None and not previous.cancel.is_set() else None
        if job is None and previous is None and result_path.exists():
            result = _refresh_cached_result(
                result_path, json.loads(result_path.read_text(encoding="utf-8"))
            )
            result["audio_index"] = audio_index
            return jsonify(status="done", **result)
        if job is None:
            task = Task(
                kind="transcode",
                message="Ожидает обработки",
                result_json=json.dumps(
                    {
                        "subject": media.name,
                        "object_title": media.media_object.title,
                        "file_id": media.id,
                    },
                    ensure_ascii=False,
                ),
            )
            db.session.add(task)
            db.session.commit()
            job = ActiveTranscode(task_id=task.id)
            _active[active_key] = job
            _executor.submit(
                _prepare_worker,
                current_app._get_current_object(),
                file_id,
                job,
                audio_index,
                bitmap_stream,
                previous,
            )
        if viewer_id is None:
            job.persistent = True
        else:
            job.viewers[viewer_id] = now
            _viewers[viewer_id] = job
    return jsonify(status="queued", task_id=job.task_id), 202


@media_bp.post("/api/playback/cancel")
def cancel_playback():
    requested = request.get_json(silent=True) or {}
    viewer_id = requested.get("viewer_id")
    if not isinstance(viewer_id, str) or not 1 <= len(viewer_id) <= 80:
        return jsonify(error="Некорректный идентификатор просмотра"), 400
    with _active_lock:
        now = time.monotonic()
        _prune_cancelled_viewers(now)
        _cancelled_viewers[viewer_id] = now
        job = _viewers.pop(viewer_id, None)
        if job is not None:
            job.viewers.pop(viewer_id, None)
            if not job.viewers and not job.persistent:
                job.cancel.set()
        wait_for_stop = job is not None and job.cancel.is_set() and job.started.is_set()
    if wait_for_stop:
        job.finished.wait(timeout=5)
    return jsonify(status="cancelled", stopped=not wait_for_stop or job.finished.is_set())


@media_bp.post("/api/playback/heartbeat")
def playback_heartbeat():
    requested = request.get_json(silent=True) or {}
    viewer_id = requested.get("viewer_id")
    task_id = requested.get("task_id")
    with _active_lock:
        job = _viewers.get(viewer_id) if isinstance(viewer_id, str) else None
        if job is None or job.task_id != task_id or job.cancel.is_set():
            return jsonify(active=False)
        job.viewers[viewer_id] = time.monotonic()
    return jsonify(active=True)


@media_bp.get("/api/tasks/<task_id>")
def task_status(task_id: str):
    task = db.get_or_404(Task, task_id)
    payload = {"status": task.status, "progress": task.progress, "message": task.message}
    if task.result_json:
        payload.update(json.loads(task.result_json))
    if task.status in {"ready", "done"} and payload.get("key"):
        playlist = Path(current_app.config["CACHE_DIR"]) / "hls" / payload["key"] / "video.m3u8"
        if task.status == "done":
            payload["prepared_until"] = payload.get("duration") or _completed_playlist_duration(
                playlist
            )
        else:
            payload["prepared_until"] = _playlist_duration(playlist)
    return jsonify(payload)


@media_bp.get("/stream/<key>/<path:filename>")
def stream_file(key: str, filename: str):
    if not key.isalnum() or PurePosixPath(filename).name != filename:
        return "", 404
    directory = Path(current_app.config["CACHE_DIR"]) / "hls" / key
    if directory.exists():
        os.utime(directory, None)
    response = send_from_directory(directory, filename, conditional=True)
    if filename.endswith(".m3u8"):
        response.mimetype = "application/vnd.apple.mpegurl"
    elif filename.endswith(".ts"):
        response.mimetype = "video/mp2t"
    elif filename.endswith(".vtt"):
        response.mimetype = "text/vtt"
    return response
