import glob
import os
import tempfile
import threading
import uuid

from flask import Flask, after_this_request, jsonify, request, send_file, send_from_directory
import yt_dlp

app = Flask(__name__)

TEMP_FOLDER = os.path.join(tempfile.gettempdir(), "ytsave")
os.makedirs(TEMP_FOLDER, exist_ok=True)

COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')

progress_data = {}


def get_cookies_opts():
    if os.path.exists(COOKIES_FILE):
        return {'cookiefile': COOKIES_FILE}
    return {}


def quality_label(height):
    if height >= 2160: return '4K (2160p)'
    if height >= 1440: return '2K (1440p)'
    if height >= 1080: return '1080p HD'
    if height >= 720:  return '720p HD'
    if height >= 480:  return '480p'
    if height >= 360:  return '360p'
    return f'{height}p'


def format_size(size):
    return f'{round(size / 1024 / 1024, 1)} MB' if size else 'Unknown'


def get_progress_hook(download_id):
    def hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded = d.get('downloaded_bytes', 0)
            percent = round(downloaded / total * 100, 1) if total else 0
            progress_data[download_id].update({
                'status': 'downloading',
                'percent': percent,
                'speed': d.get('_speed_str', '').strip(),
                'eta': d.get('_eta_str', '').strip(),
                'downloaded_mb': round(downloaded / 1024 / 1024, 1),
                'total_mb': round(total / 1024 / 1024, 1) if total else 0,
                'filename': os.path.basename(d.get('filename', ''))
            })
        elif d['status'] == 'finished':
            progress_data[download_id].update({
                'status': 'processing', 'percent': 99,
                'speed': '', 'eta': 'Processing...'
            })
        elif d['status'] == 'error':
            progress_data[download_id].update({
                'status': 'error', 'percent': 0,
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
                'status': 'finished', 'percent': 100,
                'speed': '', 'eta': '',
                'filepath': filepath,
                'filename': os.path.basename(filepath) if filepath else ''
            })
    return hook


def build_ydl_opts(download_id, fmt, ext):
    outtmpl = os.path.join(TEMP_FOLDER, f'{download_id}.%(ext)s')
    cookies = get_cookies_opts()
    hooks = {
        'progress_hooks': [get_progress_hook(download_id)],
        'postprocessor_hooks': [get_postprocessor_hook(download_id)],
        'quiet': True,
        'no_warnings': True,
    }

    if ext == 'mp3':
        return {
            'format': 'bestaudio/best',
            'outtmpl': outtmpl,
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            **hooks, **cookies
        }
    if ext == 'm4a':
        return {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': outtmpl,
            **hooks, **cookies
        }
    return {
        'format': fmt if fmt else 'bestvideo+bestaudio/best',
        'outtmpl': outtmpl,
        'merge_output_format': 'mp4',
        'postprocessor_args': {'ffmpeg': ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k']},
        **hooks, **cookies
    }


def resolve_file(download_id, ext, ydl, info):
    try:
        filepath = ydl.prepare_filename(info)
        if ext == 'mp3':
            filepath = os.path.splitext(filepath)[0] + '.mp3'
        elif ext != 'm4a':
            filepath = os.path.splitext(filepath)[0] + '.mp4'
        if os.path.exists(filepath):
            return filepath
    except Exception:
        pass
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

    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'skip_download': True, **get_cookies_opts()}) as ydl:
            info = ydl.extract_info(url, download=False)

        all_formats = info.get('formats', [])

        # Find best audio streams
        best_audio = None
        best_aac = None
        for f in all_formats:
            if f.get('vcodec') == 'none' and f.get('acodec') not in (None, 'none'):
                abr = f.get('abr') or 0
                if best_audio is None or abr > (best_audio.get('abr') or 0):
                    best_audio = f
                acodec = (f.get('acodec') or '').lower()
                if 'mp4a' in acodec or 'aac' in acodec:
                    if best_aac is None or abr > (best_aac.get('abr') or 0):
                        best_aac = f

        # Collect all video formats, deduplicated by height
        seen = set()
        video_formats = []
        for f in all_formats:
            height = f.get('height')
            if not height or f.get('vcodec', 'none') == 'none':
                continue
            if height not in seen:
                seen.add(height)
                video_formats.append(f)

        video_formats.sort(key=lambda x: x.get('height', 0), reverse=True)

        formats = []
        for f in video_formats:
            height = f.get('height')
            size = f.get('filesize') or f.get('filesize_approx')
            acodec = f.get('acodec', 'none')

            # Pick audio to merge
            audio = best_aac or best_audio
            if acodec in (None, 'none') and audio:
                fmt_id = f"{f['format_id']}+{audio['format_id']}"
            else:
                fmt_id = f['format_id']

            formats.append({
                'format_id': fmt_id,
                'type': 'video',
                'quality': quality_label(height),
                'height': height,
                'ext': 'mp4',
                'size': format_size(size),
            })

        formats.append({'format_id': 'bestaudio/best', 'type': 'audio', 'quality': 'MP3 192k', 'ext': 'mp3', 'size': '~5-10 MB'})
        formats.append({'format_id': 'bestaudio/best', 'type': 'audio', 'quality': 'M4A AAC', 'ext': 'm4a', 'size': '~5-10 MB'})

        duration_s = int(info.get('duration', 0) or 0)
        duration_str = f'{duration_s // 60}:{duration_s % 60:02d}' if duration_s else 'Unknown'
        views = info.get('view_count', 0)

        webpage_url = info.get('webpage_url', url)
        platform = 'YouTube'
        for kw, name in [('instagram', 'Instagram'), ('facebook', 'Facebook'), ('fb.com', 'Facebook'),
                         ('tiktok', 'TikTok'), ('twitter', 'Twitter/X'), ('x.com', 'Twitter/X'),
                         ('vimeo', 'Vimeo'), ('dailymotion', 'Dailymotion')]:
            if kw in webpage_url:
                platform = name
                break

        return jsonify({
            'title': info.get('title', 'Unknown Title'),
            'channel': info.get('uploader', 'Unknown'),
            'duration': duration_str,
            'thumbnail': info.get('thumbnail', ''),
            'views': f'{views:,}' if views else '',
            'platform': platform,
            'formats': formats
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 400


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
        'status': 'starting', 'percent': 0, 'speed': '', 'eta': 'Starting...',
        'downloaded_mb': 0, 'total_mb': 0, 'filename': '',
        'quality': quality, 'ext': ext, 'filepath': ''
    }

    ydl_opts = build_ydl_opts(download_id, fmt, ext)

    def run():
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = resolve_file(download_id, ext, ydl, info)
                if os.path.exists(filepath):
                    progress_data[download_id].update({
                        'status': 'finished', 'percent': 100,
                        'filepath': filepath, 'filename': os.path.basename(filepath)
                    })
                elif progress_data[download_id].get('status') not in ('finished', 'error'):
                    files = glob.glob(os.path.join(TEMP_FOLDER, f'{download_id}.*'))
                    if files:
                        progress_data[download_id].update({
                            'status': 'finished', 'percent': 100,
                            'filepath': files[0], 'filename': os.path.basename(files[0])
                        })
                    else:
                        progress_data[download_id].update({'status': 'error', 'error': 'File not found.'})
        except Exception as e:
            progress_data[download_id].update({'status': 'error', 'percent': 0, 'error': str(e)})

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'status': 'started', 'download_id': download_id})


@app.route('/progress/<download_id>')
def progress(download_id):
    return jsonify(progress_data.get(download_id, {'status': 'unknown', 'percent': 0}))


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
    filename = "".join(c for c in filename if c not in r'\/:*?"<>|').strip()
    ext = os.path.splitext(filepath)[1].lower()
    mime_map = {'.mp4': 'video/mp4', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.webm': 'video/webm'}
    mimetype = mime_map.get(ext, 'application/octet-stream')

    @after_this_request
    def cleanup(response):
        try:
            os.remove(filepath)
            progress_data.pop(download_id, None)
        except Exception:
            pass
        return response

    return send_file(filepath, mimetype=mimetype, as_attachment=True, download_name=filename)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    print(f"\nYTSave running on http://0.0.0.0:{port}\n")
    app.run(debug=False, host='0.0.0.0', port=port, threaded=True)
