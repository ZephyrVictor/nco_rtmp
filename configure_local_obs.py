"""Configure an isolated OBS profile; never replace existing profiles or scenes."""
import configparser
import json
from pathlib import Path
import secrets

root = Path.home() / 'Library/Application Support/obs-studio'
profile = root / 'basic/profiles/ICO2026'
profile.mkdir(parents=True, exist_ok=True)
recordings = Path.home() / 'Movies/ICO2026'
recordings.mkdir(parents=True, exist_ok=True)
profile_ini = f'''[General]
Name=ICO2026

[Video]
BaseCX=2560
BaseCY=1664
OutputCX=1280
OutputCY=832
FPSType=1
FPSInt=1
ScaleType=bicubic

[Output]
Mode=Advanced
FilenameFormatting=ICO2026-%CCYY-%MM-%DD_%hh-%mm-%ss

[AdvOut]
RecType=Standard
RecFormat=mkv
RecFormat2=mkv
RecEncoder=obs_x264
RecAEncoder=ffmpeg_aac
RecTracks=1
RecRescale=false
RecFilePath={recordings}
Track1Bitrate=96
Encoder=obs_x264
AudioEncoder=ffmpeg_aac
TrackIndex=1
Rescale=false

[Audio]
SampleRate=44100
ChannelSetup=Mono
'''
for filename, content in [('basic.ini',profile_ini),('recordEncoder.json',json.dumps({'rate_control':'CRF','crf':24,'profile':'high','preset':'veryfast','keyint_sec':2})),('streamEncoder.json',json.dumps({'rate_control':'CBR','bitrate':900,'profile':'high','preset':'veryfast','tune':'zerolatency','keyint_sec':2}))]:
    path=profile/filename
    if not path.exists():
        path.write_text(content)
stream_path=profile/'streamEncoder.json'
stream_settings=json.loads(stream_path.read_text())
stream_settings['tune']='zerolatency'
stream_path.write_text(json.dumps(stream_settings))
settings=root/'global.ini'
config=configparser.ConfigParser(interpolation=None)
config.optionxform=str
if settings.exists():
    config.read(settings)
    backup=settings.with_suffix('.pre-ico2026.ini')
    if not backup.exists():
        backup.write_bytes(settings.read_bytes())
for section in ['Basic','General','BasicWindow']:
    if section not in config: config[section]={}
config['Basic'].update({'Profile':'ICO2026','ProfileDir':'ICO2026','SceneCollection':'ICO2026','SceneCollectionFile':'ICO2026'})
config['General'].update({'FirstRun':'false','Pre19Defaults':'false','Pre21Defaults':'false','Pre23Defaults':'false','Pre24.1Defaults':'false'})
config['BasicWindow']['PreviewEnabled']='true'
with settings.open('w') as target: config.write(target,space_around_delimiters=False)
scene_dir=root/'basic/scenes'
target_scene=scene_dir/'ICO2026.json'
template=scene_dir/'characters_only.json'
if not target_scene.exists() and template.exists():
    collection=json.loads(template.read_text())
    source=next((s for s in collection.get('sources',[]) if s.get('name')=='ICO2026 Display'),None)
    scene=next((s for s in collection.get('sources',[]) if s.get('id')=='scene' and s.get('name')=='ICO2026'),None)
    if source and scene:
        source['settings']['display_uuid']=''
        collection['name']='ICO2026'
        collection['sources']=[source,scene]
        collection['scene_order']=[{'name':'ICO2026'}]
        collection['current_scene']=collection['current_program_scene']='ICO2026'
        for name in list(collection):
            if name.startswith(('AuxAudioDevice','DesktopAudioDevice')):
                collection.pop(name)
        target_scene.write_text(json.dumps(collection,ensure_ascii=False,indent=2))
ws=root/'plugin_config/obs-websocket/config.json'
ws.parent.mkdir(parents=True,exist_ok=True)
if not ws.exists():
    ws.write_text(json.dumps({'server_enabled':True,'server_port':4455,'auth_required':True,'server_password':secrets.token_urlsafe(32),'alerts_enabled':False}))
    ws.chmod(0o600)
print('OBS profile ready:',profile)
print('Local recordings:',recordings)
