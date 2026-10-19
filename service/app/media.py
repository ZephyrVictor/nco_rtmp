import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import av

from .core import DATA, RULES, safe_path


def run_json(args, timeout=120):
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        result = subprocess.run(args, stdout=output, stderr=error, timeout=timeout)
        if result.returncode:
            error.seek(0)
            raise ValueError('Media probe failed: '+error.read(4000).decode(errors='replace'))
        if output.tell()>4*1024**2:
            raise ValueError('Media metadata exceeds validation limit')
        output.seek(0)
        return json.load(output)


def sha256(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(4 * 1024**2), b''):
            value.update(block)
    return value.hexdigest()


def validate(item, upload=None):
    path = safe_path(item['path'])
    # Keep tus HEAD/resume receipts valid by retaining the spool inode.
    if item['kind'] == 'original':
        target = DATA / 'originals' / item['contestant_id'] / item['round_id'] / (item['id'] + '.mkv')
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            os.link(path, target)
        elif not os.path.samefile(path, target):
            raise ValueError('Original destination already exists with different content')
        path = target
        path.chmod(0o440)
    checksum = sha256(path)
    report = {'sha256': checksum, 'errors': [], 'warnings': [], 'rules': RULES,
              'automatic_compliance': False,
              'manual_checks': ['宿主机完整屏幕、无裁切或遮挡', '覆盖实际比赛时间且无暂停或缺口', '异常已立即通报志愿者'],
              'path': str(path.relative_to(DATA))}
    if upload and checksum != upload['expected_sha256']:
        report['errors'].append('SHA-256 与客户端提交值不一致')
    demuxer = 'matroska' if item['kind']=='original' else ('flv' if '.flv' in path.name else 'mov')
    probe = run_json(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-f', demuxer,
                     '-show_format', '-show_streams', '-of', 'json', str(path)])
    report['probe'] = probe
    videos = [s for s in probe['streams'] if s['codec_type'] == 'video']
    audios = [s for s in probe['streams'] if s['codec_type'] == 'audio']
    duration = float(probe.get('format', {}).get('duration', 0))
    report['duration_seconds'] = duration
    if len(videos) != 1:
        report['errors'].append('须有且仅有一个视频轨道')
    if not videos:
        raise ValueError('No video stream')
    video = videos[0]
    if video.get('width',0)>8192 or video.get('height',0)>8192:
        raise ValueError('Video dimensions exceed the validation resource limit')
    report['x264_options'] = None
    try:
        with av.open(str(path)) as container:
            container.streams.video[0].codec_context.thread_count = 1
            for frame in container.decode(video=0):
                for side in frame.side_data:
                    if 'SEI_UNREGISTERED' in str(side.type):
                        message = bytes(side).decode('utf-8', errors='replace')
                        if 'x264' in message:
                            report['x264_options'] = message[16:]
                break
    except Exception as error:
        report['warnings'].append('编码器 SEI 读取失败: ' + str(error)[:200])
    if item['kind'] == 'original':
        checks = [
            ('matroska' in probe.get('format', {}).get('format_name', ''), '容器必须是 MKV'),
            (video.get('codec_name') == 'h264', '视频必须是 H.264/x264'),
            (video.get('profile') == 'High', '视频 Profile 必须是 High'),
            (video.get('width', 0) >= 1280 and video.get('height', 0) >= 720, '输出分辨率至少 1280×720'),
            (video.get('avg_frame_rate') == '1/1' and video.get('r_frame_rate') == '1/1', '帧率必须为 1 FPS'),
            (len(audios) == 1 and audios[0].get('codec_name') == 'aac', '音轨必须为 AAC'),
            (len(audios) == 1 and audios[0].get('sample_rate') == '44100', '音频采样率必须为 44100 Hz'),
            (len(audios) == 1 and audios[0].get('channels') == 1, '音频必须为单声道'),
            (duration >= RULES['round_seconds'] - 1, '录像不足单场 7 小时，需核对全部原件和异常记录'),
        ]
        for valid, message in checks:
            if not valid:
                report['errors'].append(message)
        options = report['x264_options']
        if options:
            if not re.search(r'\brc=crf\b', options) or not re.search(r'\bcrf=24(?:\.0)?(?:\s|$)', options):
                report['errors'].append('x264 编码器参数不是 CRF 24')
        else:
            report['warnings'].append('未提取到 x264 CRF 信息，须核对 OBS 配置和原始日志')
    # Decode the entire video AND audio; never infer integrity from ffprobe alone.
    with open(os.devnull, 'wb') as sink:
        result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-threads', '1',
                                 '-protocol_whitelist','file,pipe','-f',demuxer,
                                 '-i', str(path), '-map', '0:v', '-map', '0:a?', '-threads', '1',
                                 '-f', 'null', '-'], stdout=sink, stderr=subprocess.PIPE, timeout=14400)
    report['decode_ok'] = result.returncode == 0
    if not report['decode_ok']:
        report['errors'].append('完整解码失败: ' + result.stderr.decode(errors='replace')[-3000:])
    report['parameters_pass'] = not report['errors']
    return report
