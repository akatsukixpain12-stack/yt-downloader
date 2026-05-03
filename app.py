import glob
import os
import tempfile
import threading
import uuid

from flask import Flask, after_this_request, jsonify, request, send_file, send_from_directory
import yt_dlp

app = Flask(__name__)

@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

TEMP_FOLDER = os.path.join(tempfile.gettempdir(), "ytsave")
os.makedirs(TEMP_FOLDER, exist_ok=True)

progress_data = {}


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
            if filepath and os.path.exists(filepath):
                progress_data[download_id].update({
                    'status': 'finished', 'percent': 100,
                    'speed': '', 'eta': '',
                    'filepath': filepath,
                    'filename': os.path.basename(filepath)
                })
    return hook


def make_format_string(height):
    """Height-based format string with multiple fallbacks — never fails."""
    if height and int(height) > 0:
        h = int(height)
        return (
            f'bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/'
            f'bestvideo[height<={h}][ext=mp4]+bestaudio/'
            f'bestvideo[height<={h}]+bestaudio[ext=m4a]/'
            f'bestvideo[height<={h}]+bestaudio/'
            f'best[height<={h}]/'
            f'bestvideo+bestaudio/'
            f'best'
        )
    return 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best'


def base_opts(download_id):
    return {
        'progress_hooks': [get_progress_hook(download_id)],
        'postprocessor_hooks': [get_postprocessor_hook(download_id)],
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
    }


def find_output_file(download_id, ext):
    for e in [ext, 'mp4', 'mkv', 'webm', 'mp3', 'm4a']:
        path = os.path.join(TEMP_FOLDER, f'{download_id}.{e}')
        if os.path.exists(path):
            return path
    files = glob.glob(os.path.join(TEMP_FOLDER, f'{download_id}.*'))
    files = [f for f in files if not f.endswith('.part') and not f.endswith('.ytdl')]
    return files[0] if files else ''


@app.route('/sitemap.xml')
def sitemap():
    return send_from_directory('templates', 'sitemap.xml', mimetype='application/xml')


@app.route('/robots.txt')
def robots():
    return "User-agent: *\nAllow: /\nSitemap: https://ytsave.onrender.com/sitemap.xml\n", 200, {'Content-Type': 'text/plain'}


@app.route('/')
def index():
    return send_from_directory('templates', 'index.html')


@app.route('/healthz')
def healthz():
    return jsonify({'ok': True})


@app.route('/info', methods=['POST'])
def get_info():
    data = request.get_json()
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    try:
        with yt_dlp.YoutubeDL({
            'quiet': True, 'no_warnings': True,
            'skip_download': True, 'noplaylist': True,
        }) as ydl:
            info = ydl.extract_info(url, download=False)

        all_formats = info.get('formats', [])
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
            formats.append({
                'height': height,
                'type': 'video',
                'quality': quality_label(height),
                'ext': 'mp4',
                'size': format_size(size),
            })

        formats.append({'height': 0, 'type': 'audio', 'quality': 'MP3 192k', 'ext': 'mp3', 'size': '~5-10 MB'})
        formats.append({'height': 0, 'type': 'audio', 'quality': 'M4A AAC', 'ext': 'm4a', 'size': '~5-10 MB'})

        duration_s = int(info.get('duration', 0) or 0)
        duration_str = f'{duration_s // 60}:{duration_s % 60:02d}' if duration_s else 'Unknown'
        views = info.get('view_count', 0)

        webpage_url = info.get('webpage_url', url)
        platform = 'YouTube'
        for kw, name in [('instagram','Instagram'),('facebook','Facebook'),('fb.com','Facebook'),
                         ('tiktok','TikTok'),('twitter','Twitter/X'),('x.com','Twitter/X'),
                         ('vimeo','Vimeo'),('dailymotion','Dailymotion')]:
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
    url = (data.get('url') or '').strip()
    height = data.get('height', 0)
    ext = data.get('ext', 'mp4')
    quality = data.get('quality', '')
    download_id = str(uuid.uuid4())

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    progress_data[download_id] = {
        'status': 'starting', 'percent': 0, 'speed': '',
        'eta': 'Starting...', 'downloaded_mb': 0, 'total_mb': 0,
        'filename': '', 'quality': quality, 'ext': ext, 'filepath': ''
    }

    outtmpl = os.path.join(TEMP_FOLDER, f'{download_id}.%(ext)s')
    opts = base_opts(download_id)

    if ext == 'mp3':
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': outtmpl,
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            **opts
        }
    elif ext == 'm4a':
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': outtmpl,
            **opts
        }
    else:
        ydl_opts = {
            'format': make_format_string(height),
            'outtmpl': outtmpl,
            'merge_output_format': 'mp4',
            'postprocessor_args': {'ffmpeg': ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k']},
            **opts
        }

    def run():
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            filepath = find_output_file(download_id, ext)
            if filepath:
                progress_data[download_id].update({
                    'status': 'finished', 'percent': 100,
                    'filepath': filepath,
                    'filename': os.path.basename(filepath)
                })
            elif progress_data[download_id].get('status') not in ('finished', 'error'):
                progress_data[download_id].update({
                    'status': 'error', 'error': 'File not found after download.'
                })
        except Exception as e:
            progress_data[download_id].update({
                'status': 'error', 'percent': 0, 'error': str(e)
            })

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
        filepath = find_output_file(download_id, d.get('ext', 'mp4'))
        if not filepath:
            return jsonify({'error': 'File not found'}), 404

    filename = d.get('filename', os.path.basename(filepath))
    filename = "".join(c for c in filename if c not in r'\/:*?"<>|').strip() or f'download.{d.get("ext","mp4")}'

    mime_map = {'.mp4':'video/mp4','.mp3':'audio/mpeg','.m4a':'audio/mp4','.webm':'video/webm'}
    mimetype = mime_map.get(os.path.splitext(filepath)[1].lower(), 'application/octet-stream')

    @after_this_request
    def cleanup(response):
        try:
            os.remove(filepath)
            progress_data.pop(download_id, None)
        except Exception:
            pass
        return response

    return send_file(filepath, mimetype=mimetype, as_attachment=True,
                     download_name=filename, conditional=False)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    print(f"\nYTSave running on http://0.0.0.0:{port}\n")
    app.run(debug=False, host='0.0.0.0', port=port, threaded=True)
