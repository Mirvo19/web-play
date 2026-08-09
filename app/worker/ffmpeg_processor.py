import os
import json
import subprocess
import shutil
from typing import Dict, Any, List, Tuple

RESOLUTION_LADDER = [
    (3840, 2160, '2160p'),
    (2560, 1440, '1440p'),
    (1920, 1080, '1080p'),
    (1280, 720,  '720p'),
    (854,  480,  '480p'),
    (640,  360,  '360p'),
    (426,  240,  '240p')
]

def inspect_video(file_path: str, ffprobe_bin: str = None) -> Dict[str, Any]:
    """Inspect video file metadata using ffprobe."""
    ffprobe_bin = ffprobe_bin or shutil.which('ffprobe')
    if not ffprobe_bin:
        raise RuntimeError('FFprobe executable not found. Please install ffprobe or set full path in FFMPEG_BIN/FFPROBE_BIN environment variables.')

    cmd = [
        ffprobe_bin,
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_format',
        '-show_streams',
        file_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    probe_data = json.loads(result.stdout)

    width = 0
    height = 0
    fps = 30.0
    duration = 0.0
    has_audio = False
    bitrate = 0
    video_codec = None
    audio_codec = None
    format_name = None

    format_data = probe_data.get('format', {})
    duration = float(format_data.get('duration', 0.0))
    bitrate = int(format_data.get('bit_rate', 0))
    format_name = format_data.get('format_name')

    for stream in probe_data.get('streams', []):
        codec_type = stream.get('codec_type')
        if codec_type == 'video' and width == 0 and height == 0:
            width = int(stream.get('width', 0) or 0)
            height = int(stream.get('height', 0) or 0)
            fps_str = stream.get('r_frame_rate', '30/1')
            if '/' in fps_str:
                num, den = fps_str.split('/')
                fps = float(num) / float(den) if float(den) > 0 else 30.0
            else:
                fps = float(fps_str) if fps_str else 30.0
            video_codec = stream.get('codec_name')
        elif codec_type == 'audio':
            has_audio = True
            audio_codec = stream.get('codec_name')

    if width == 0 or height == 0:
        raise RuntimeError('Unable to determine video resolution from source file.')

    return {
        'width': width,
        'height': height,
        'duration': duration,
        'fps': round(fps, 2),
        'bitrate': bitrate,
        'size': int(format_data.get('size', os.path.getsize(file_path))),
        'has_audio': has_audio,
        'video_codec': video_codec,
        'audio_codec': audio_codec,
        'format_name': format_name
    }


def determine_quality_targets(source_width: int, source_height: int) -> List[Dict[str, Any]]:
    """
    Quality Rule: Original Quality + Exactly One Step Down.
    Never upscale.
    """
    targets = []
    
    # 1. Original Quality
    orig_res_label = f"{source_height}p"
    # Find matching label if height matches standard ladder
    for w, h, label in RESOLUTION_LADDER:
        if abs(source_height - h) <= 10:
            orig_res_label = label
            break

    targets.append({
        'label': orig_res_label,
        'width': source_width,
        'height': source_height,
        'is_original': True
    })

    # 2. Step Down Quality
    step_down = None
    for w, h, label in RESOLUTION_LADDER:
        if h < source_height:
            # Calculate target width maintaining aspect ratio
            aspect_ratio = source_width / source_height if source_height > 0 else 16/9
            target_width = int(h * aspect_ratio)
            # Ensure width is even for libx264
            if target_width % 2 != 0:
                target_width -= 1
            step_down = {
                'label': label,
                'width': target_width,
                'height': h,
                'is_original': False
            }
            break

    if step_down:
        targets.append(step_down)

    return targets


def build_ffmpeg_transcode_command(
    input_path: str,
    output_dir: str,
    target: Dict[str, Any],
    meta: Dict[str, Any],
    threads: int = 40,
    preset: str = 'veryfast',
    crf: int = 23,
    segment_duration: int = 6
) -> Tuple[List[str], str]:
    """Build FFmpeg command to produce HLS variant playlist."""
    os.makedirs(output_dir, exist_ok=True)
    playlist_path = os.path.join(output_dir, 'playlist.m3u8')
    segment_filename_pattern = os.path.join(output_dir, 'segment_%04d.ts')

    width = target['width']
    height = target['height']
    fps = max(1, round(meta.get('fps', 30.0)))
    keyint = max(1, int(segment_duration * fps))

    ffmpeg_bin = shutil.which('ffmpeg') or 'ffmpeg'
    cmd = [
        ffmpeg_bin,
        '-y',
        '-progress', 'pipe:1',
        '-nostats',
        '-threads', str(max(1, threads)),
        '-i', input_path,
        '-map', '0:v:0'
    ]

    if meta.get('has_audio'):
        cmd += ['-map', '0:a:0?']

    if target.get('is_original') and meta.get('video_codec') == 'h264' and meta.get('width') == width and meta.get('height') == height:
        cmd += ['-c:v', 'copy']
    else:
        cmd += [
            '-c:v', 'libx264',
            '-preset', preset,
            '-crf', str(crf),
            '-x264-params', f'keyint={keyint}:scenecut=0',
            '-vf', f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad=ceil(iw/2)*2:ceil(ih/2)*2',
            '-pix_fmt', 'yuv420p'
        ]

    if meta.get('has_audio'):
        if meta.get('audio_codec') == 'aac':
            cmd += ['-c:a', 'copy']
        else:
            cmd += ['-c:a', 'aac', '-b:a', '128k', '-ac', '2']
    else:
        cmd += ['-an']

    cmd += [
        '-g', str(keyint),
        '-keyint_min', str(keyint),
        '-sc_threshold', '0',
        '-hls_time', str(segment_duration),
        '-hls_playlist_type', 'vod',
        '-hls_segment_filename', segment_filename_pattern,
        playlist_path
    ]

    return cmd, playlist_path


def extract_thumbnail(input_path: str, output_path: str, timestamp_sec: float = 1.0) -> bool:
    """Extract a thumbnail image at given timestamp."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    ffmpeg_bin = shutil.which('ffmpeg') or 'ffmpeg'
    cmd = [
        ffmpeg_bin,
        '-y',
        '-ss', str(timestamp_sec),
        '-i', input_path,
        '-vframes', '1',
        '-q:v', '2',
        output_path
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.returncode == 0 and os.path.exists(output_path)
    except Exception:
        return False
