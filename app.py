import glob
import os
import tempfile
import threading
import uuid

from flask import Flask, after_this_request, jsonify, request, send_file, send_from_directory
import yt_dlp

app = Flask(__name__)

# Use ephemeral temp storage so the app stays stateless for managed hosting.
TEMP_FOLDER = os.path.join(tempfile.gettempdir(), "ytsave")
os.makedirs(TEMP_FOLDER, exist_ok=True)

# Track progress and file path per download ID
progress_data = {}


def is_mp4_compatible_video(fmt):
    ext = (fmt.get('ext') or '').lower()
    vcodec = (fmt.get('vcodec') or '').lower()
    return (
        ext == 'mp4'
        and vcodec not in ('none', '')
        and not vcodec.startswith('vp')
        and 'av01' not in vcodec
    )


def is_mp4_compatible_audio(fmt):
    ext = (fmt.get('ext') or '').lower()
    acodec = (fmt.get('acodec') or '').lower()
    return (
        fmt.get('vcodec') == 'none'
        and acodec not in ('none', '')
        and (ext == 'm4a' or 'mp4a' in acodec or 'aac' in acodec)
    )


def quality_label(height):
    if height >= 2160:
        return '4K (2160p)'
    if height >= 1440:
        return '2K (1440p)'
    if height >= 1080:
        return '1080p HD'
    if height >= 720:
        return '720p HD'
    if height >= 480:
        return '480p'
    if height >= 360:
        return '360p'
    return f'{height}p'


def format_size(size):
    return f'{round(size / 1024 / 1024, 1)} MB' if size else 'Unknown'


def get_progress_hook(download_id):
    def hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded = d.get('downloaded_bytes', 0)
            percent = round(downloaded / total * 100, 1) if total else 0
            speed = d.get('_speed_str', '').strip()
            eta = d.get('_eta_str', '').strip()
            downloaded_mb = round(downloaded / 1024 / 1024, 1)
            total_mb = round(total / 1024 / 1024, 1) if total else 0
            progress_data[download_id].update({
                'status': 'downloading',
                'percent': percent,
                'speed': speed,
                'eta': eta,
                'downloaded_mb': downloaded_mb,
                'total_mb': total_mb,
                'filename': os.path.basename(d.get('filename', ''))
            })
        elif d['status'] == 'finished':
            progress_data[download_id].update({
                'status': 'processing',
                'percent': 99,
                'speed': '',
                'eta': 'Processing...',
            })
        elif d['status'] == 'error':
            progress_data[download_id].update({
                'status': 'error',
                'percent': 0,
                'error': str(d.get('error', 'Unknown error'))
            })

    return hook


def get_postprocessor_hook(download_id):
    def hook(d):
        if d['status'] == 'finished':
            filepath = d.get('info_dict', {}).get('filepath', '')
            if not filepath or not os.path.exists(filepath):
                files = glob.glob(os.path.join(TEMP_FOLDER, f"{download_id}.*"))
                filepath = files[0] if files else ''
            progress_data[download_id].update({
                'status': 'finished',
                'percent': 100,
                'speed': '',
                'eta': '',
                'filepath': filepath,
                'filename': os.path.basename(filepath) if filepath else ''
            })

    return hook


COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')

def get_cookies_opts():
    """Use cookies.txt if present to bypass bot detection on cloud servers."""
    if os.path.exists(COOKIES_FILE):
        return {'cookiefile': COOKIES_FILE}
    return {}

def build_download_options(download_id, fmt, ext):
    outtmpl = os.path.join(TEMP_FOLDER, f'{download_id}.%(ext)s')
    cookies = get_cookies_opts()

    if ext == 'mp3':
        return {
            'format': 'bestaudio/best',
            'outtmpl': outtmpl,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'progress_hooks': [get_progress_hook(download_id)],
            'postprocessor_hooks': [get_postprocessor_hook(download_id)],
            'quiet': True,
            'no_warnings': True,
            **cookies,
        }

    if ext == 'm4a':
        return {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': outtmpl,
            'progress_hooks': [get_progress_hook(download_id)],
            'postprocessor_hooks': [get_postprocessor_hook(download_id)],
            'quiet': True,
            'no_warnings': True,
            **cookies,
        }

    return {
        'format': fmt if fmt else 'bestvideo+bestaudio/best',
        'outtmpl': outtmpl,
        'merge_output_format': 'mp4',
        'postprocessor_args': {
            'ffmpeg': ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k']
        },
        'progress_hooks': [get_progress_hook(download_id)],
        'postprocessor_hooks': [get_postprocessor_hook(download_id)],
        'quiet': True,
        'no_warnings': True,
        **cookies,
    }


def resolve_downloaded_file(download_id, ext, ydl, info):
    filepath = ydl.prepare_filename(info)
    if ext not in ('mp3', 'm4a'):
        filepath = os.path.splitext(filepath)[0] + '.mp4'
    if ext == 'mp3':
        filepath = os.path.splitext(filepath)[0] + '.mp3'

    if os.path.exists(filepath):
        return filepath

    files = glob.glob(os.path.join(TEMP_FOLDER, f'{download_id}.*'))
    return files[0] if files else ''


@app.route('/')
def index():
    return send_from_directory('templates', 'index.html')


@app.route('/healthz')
def healthz():
    return jsonify({'ok': True})


@app.route('/info', methods=['POST'])
def get_info():
    data = request.get_json()
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        **get_cookies_opts(),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = []
        seen_heights = set()
        all_formats = info.get('formats', [])

        best_audio = None
        best_mp4_audio = None
        for item in all_formats:
            if item.get('vcodec') == 'none' and item.get('acodec') != 'none':
                if best_audio is None or (item.get('abr') or 0) > (best_audio.get('abr') or 0):
                    best_audio = item
                if is_mp4_compatible_audio(item):
                    if best_mp4_audio is None or (item.get('abr') or 0) > (best_mp4_audio.get('abr') or 0):
                        best_mp4_audio = item

        video_formats = []
        fallback_formats = []
        for item in all_formats:
            height = item.get('height')
            if not height:
                continue
            if item.get('vcodec', 'none') == 'none':
                continue
            if is_mp4_compatible_video(item) and height not in seen_heights:
                seen_heights.add(height)
                video_formats.append(item)
            elif height not in seen_heights:
                fallback_formats.append(item)

        video_formats.sort(key=lambda item: item.get('height', 0), reverse=True)
        fallback_formats.sort(key=lambda item: item.get('height', 0), reverse=True)

        source_video_formats = video_formats or fallback_formats

        for item in source_video_formats:
            height = item.get('height')
            size = item.get('filesize') or item.get('filesize_approx')
            acodec = item.get('acodec', 'none')
            is_universal = is_mp4_compatible_video(item) and best_mp4_audio is not None

            if acodec == 'none':
                selected_audio = best_mp4_audio if is_universal else best_audio
                fmt_id = f"{item['format_id']}+{selected_audio['format_id']}" if selected_audio else item['format_id']
            else:
                fmt_id = item['format_id']

            formats.append({
                'format_id': fmt_id,
                'type': 'video',
                'quality': quality_label(height),
                'height': height,
                'ext': 'mp4',
                'size': format_size(size),
                'codec': item.get('vcodec', 'unknown'),
                'audio_codec': (
                    item.get('acodec')
                    if item.get('acodec') not in (None, 'none')
                    else (best_mp4_audio or best_audio or {}).get('acodec', 'unknown')
                ),
                'compatibility': 'Universal MP4' if is_universal else 'High quality MP4',
                'note': 'Best browser/device support' if is_universal else 'May depend on source codecs'
            })

        formats.append({
            'format_id': 'bestaudio/best',
            'type': 'audio',
            'quality': 'MP3 192k',
            'ext': 'mp3',
            'size': '~5-10 MB',
            'compatibility': 'Universal audio',
            'note': 'Extracted with FFmpeg'
        })
        formats.append({
            'format_id': 'bestaudio[ext=m4a]/bestaudio/best',
            'type': 'audio',
            'quality': 'M4A AAC',
            'ext': 'm4a',
            'size': '~5-10 MB',
            'compatibility': 'Apple/Android friendly',
            'note': 'Better native playback support'
        })

        duration_s = info.get('duration', 0) or 0
        minutes = int(duration_s) // 60
        seconds = int(duration_s) % 60
        duration_str = f'{minutes}:{seconds:02d}' if duration_s else 'Unknown'

        views = info.get('view_count', 0)
        views_str = f'{views:,}' if views else ''

        webpage_url = info.get('webpage_url', url)
        platform = 'YouTube'
        if 'instagram' in webpage_url:
            platform = 'Instagram'
        elif 'facebook' in webpage_url or 'fb.com' in webpage_url:
            platform = 'Facebook'
        elif 'tiktok' in webpage_url:
            platform = 'TikTok'
        elif 'twitter' in webpage_url or 'x.com' in webpage_url:
            platform = 'Twitter/X'
        elif 'vimeo' in webpage_url:
            platform = 'Vimeo'
        elif 'dailymotion' in webpage_url:
            platform = 'Dailymotion'

        return jsonify({
            'title': info.get('title', 'Unknown Title'),
            'channel': info.get('uploader', 'Unknown'),
            'duration': duration_str,
            'thumbnail': info.get('thumbnail', ''),
            'views': views_str,
            'platform': platform,
            'formats': formats,
            'best_audio_codec': (best_mp4_audio or best_audio or {}).get('acodec', ''),
            'supports_universal_mp4': bool(best_mp4_audio and video_formats)
        })
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/download', methods=['POST'])
def download():
    data = request.get_json()
    url = data.get('url', '').strip()
    fmt = data.get('format_id', 'bestvideo+bestaudio/best')
    ext = data.get('ext', 'mp4')
    quality = data.get('quality', '')
    download_id = str(uuid.uuid4())

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    progress_data[download_id] = {
        'status': 'starting',
        'percent': 0,
        'speed': '',
        'eta': 'Starting...',
        'downloaded_mb': 0,
        'total_mb': 0,
        'filename': '',
        'quality': quality,
        'ext': ext,
        'filepath': ''
    }

    ydl_opts = build_download_options(download_id, fmt, ext)

    def run_download():
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = resolve_downloaded_file(download_id, ext, ydl, info)

                if os.path.exists(filepath):
                    progress_data[download_id].update({
                        'status': 'finished',
                        'percent': 100,
                        'speed': '',
                        'eta': '',
                        'filepath': filepath,
                        'filename': os.path.basename(filepath)
                    })
                elif progress_data[download_id].get('status') not in ('finished', 'error'):
                    files = glob.glob(os.path.join(TEMP_FOLDER, f'{download_id}.*'))
                    if files:
                        filepath = files[0]
                        progress_data[download_id].update({
                            'status': 'finished',
                            'percent': 100,
                            'filepath': filepath,
                            'filename': os.path.basename(filepath)
                        })
                    else:
                        progress_data[download_id].update({'status': 'error', 'error': 'File not found after download.'})
        except Exception as exc:
            progress_data[download_id].update({
                'status': 'error',
                'percent': 0,
                'error': str(exc)
            })

    thread = threading.Thread(target=run_download, daemon=True)
    thread.start()

    return jsonify({'status': 'started', 'download_id': download_id})


@app.route('/download-file', methods=['POST'])
def download_file():
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    fmt = data.get('format_id', 'bestvideo+bestaudio/best')
    ext = data.get('ext', 'mp4')
    download_id = str(uuid.uuid4())

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    progress_data[download_id] = {
        'status': 'starting',
        'percent': 0,
        'speed': '',
        'eta': 'Starting...',
        'downloaded_mb': 0,
        'total_mb': 0,
        'filename': '',
        'quality': data.get('quality', ''),
        'ext': ext,
        'filepath': ''
    }

    ydl_opts = build_download_options(download_id, fmt, ext)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = resolve_downloaded_file(download_id, ext, ydl, info)

        if not filepath or not os.path.exists(filepath):
            progress_data.pop(download_id, None)
            return jsonify({'error': 'File not found after download.'}), 500

        filename = "".join(
            char for char in os.path.basename(filepath) if char not in r'\/:*?"<>|'
        ).strip()
        mimetype = {
            '.mp4': 'video/mp4',
            '.mp3': 'audio/mpeg',
            '.m4a': 'audio/mp4',
            '.webm': 'video/webm',
            '.mkv': 'video/x-matroska',
        }.get(os.path.splitext(filepath)[1].lower(), 'application/octet-stream')

        @after_this_request
        def cleanup(response):
            try:
                os.remove(filepath)
                progress_data.pop(download_id, None)
            except Exception:
                pass
            return response

        return send_file(
            filepath,
            mimetype=mimetype,
            as_attachment=True,
            download_name=filename
        )
    except Exception as exc:
        progress_data.pop(download_id, None)
        return jsonify({'error': str(exc)}), 400


@app.route('/progress/<download_id>')
def progress(download_id):
    data = progress_data.get(download_id, {'status': 'unknown', 'percent': 0})
    return jsonify(data)


@app.route('/file/<download_id>')
def serve_file(download_id):
    d = progress_data.get(download_id)
    if not d or d.get('status') != 'finished':
        return jsonify({'error': 'File not ready'}), 404

    filepath = d.get('filepath', '')
    if not filepath or not os.path.exists(filepath):
        files = glob.glob(os.path.join(TEMP_FOLDER, f'{download_id}.*'))
        if not files:
            return jsonify({'error': 'File not found'}), 404
        filepath = files[0]

    filename = d.get('filename', os.path.basename(filepath))
    filename = "".join(char for char in filename if char not in r'\/:*?"<>|').strip()

    ext = os.path.splitext(filepath)[1].lower()
    mime_map = {
        '.mp4': 'video/mp4',
        '.mp3': 'audio/mpeg',
        '.m4a': 'audio/mp4',
        '.webm': 'video/webm',
        '.mkv': 'video/x-matroska',
    }
    mimetype = mime_map.get(ext, 'application/octet-stream')

    @after_this_request
    def cleanup(response):
        try:
            os.remove(filepath)
            progress_data.pop(download_id, None)
        except Exception:
            pass
        return response

    return send_file(
        filepath,
        mimetype=mimetype,
        as_attachment=True,
        download_name=filename
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    print("\nYTSave is running")
    print(f"Temp folder: {TEMP_FOLDER}")
    print(f"Listening on: http://0.0.0.0:{port}\n")
    app.run(debug=False, host='0.0.0.0', port=port, threaded=True)
