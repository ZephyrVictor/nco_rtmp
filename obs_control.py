import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path

import websockets


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['inspect','configure','record','stop','screenshot','stream','stop-stream'])
    parser.add_argument('--seconds',type=int,default=30)
    parser.add_argument('--service-file')
    args=parser.parse_args()
    config=Path.home()/'Library/Application Support/obs-studio/plugin_config/obs-websocket/config.json'
    settings=json.loads(config.read_text())
    async with websockets.connect('ws://127.0.0.1:'+str(settings['server_port'])) as ws:
        hello=json.loads(await ws.recv())['d']
        identify={'rpcVersion':1,'eventSubscriptions':0}
        if 'authentication' in hello:
            auth=hello['authentication']
            value=base64.b64encode(hashlib.sha256((settings['server_password']+auth['salt']).encode()).digest()).decode()
            identify['authentication']=base64.b64encode(hashlib.sha256((value+auth['challenge']).encode()).digest()).decode()
        await ws.send(json.dumps({'op':1,'d':identify}))
        assert json.loads(await ws.recv())['op']==2
        async def request(kind,data=None):
            await ws.send(json.dumps({'op':6,'d':{'requestType':kind,'requestId':kind,'requestData':data or {}}}))
            response=json.loads(await ws.recv())['d']
            if not response['requestStatus']['result']:
                raise RuntimeError(str(response['requestStatus']))
            return response.get('responseData',{})
        if args.action=='inspect':
            for kind in ['GetVersion','GetVideoSettings','GetProfileList','GetSceneList','GetInputList','GetRecordStatus','GetInputKindList']:
                print(kind,json.dumps(await request(kind),ensure_ascii=False))
            print('Screen defaults',await request('GetInputDefaultSettings',{'inputKind':'screen_capture'}))
        if args.action=='configure':
            collections=await request('GetSceneCollectionList')
            if 'ICO2026' not in collections['sceneCollections']:
                await request('CreateSceneCollection',{'sceneCollectionName':'ICO2026'})
            elif collections['currentSceneCollectionName']!='ICO2026':
                await request('SetCurrentSceneCollection',{'sceneCollectionName':'ICO2026'})
            scenes=await request('GetSceneList')
            if not any(s['sceneName']=='ICO2026' for s in scenes['scenes']):
                await request('CreateScene',{'sceneName':'ICO2026'})
            inputs=await request('GetInputList')
            capture={'type':0,'show_cursor':True,'hide_obs':False,'display_uuid':''}
            if not any(i['inputName']=='ICO2026 Display' for i in inputs['inputs']):
                await request('CreateInput',{'sceneName':'ICO2026','inputName':'ICO2026 Display','inputKind':'screen_capture','inputSettings':capture,'sceneItemEnabled':True})
            else:
                await request('SetInputSettings',{'inputName':'ICO2026 Display','inputSettings':capture,'overlay':True})
            item=await request('GetSceneItemId',{'sceneName':'ICO2026','sourceName':'ICO2026 Display'})
            await request('SetSceneItemTransform',{'sceneName':'ICO2026','sceneItemId':item['sceneItemId'],
                          'sceneItemTransform':{'positionX':1280,'positionY':832,'alignment':0,
                          'boundsType':'OBS_BOUNDS_SCALE_INNER','boundsWidth':2560,'boundsHeight':1664,
                          'cropLeft':0,'cropRight':0,'cropTop':0,'cropBottom':0,'rotation':0}})
            await request('SetCurrentProgramScene',{'sceneName':'ICO2026'})
            for kind,data in [('GetInputSettings',{'inputName':'ICO2026 Display'}),('GetSceneItemList',{'sceneName':'ICO2026'})]:
                print(kind,await request(kind,data))
        if args.action=='record':
            await request('StartRecord')
            await asyncio.sleep(args.seconds)
            print(await request('StopRecord'))
        if args.action=='stop':
            print(await request('StopRecord'))
        if args.action=='screenshot':
            print(await request('SaveSourceScreenshot',{'sourceName':'ICO2026','imageFormat':'png','imageFilePath':str(Path(__file__).resolve().parent/'obs-source-screenshot.png'),'imageWidth':1280,'imageHeight':832}))
        if args.action=='stream':
            service=json.loads(Path(args.service_file).read_text())
            await request('SetStreamServiceSettings',{'streamServiceType':'rtmp_custom','streamServiceSettings':{'server':service['server'],'key':service['stream_key'],'use_auth':False}})
            print(await request('StartStream'))
        if args.action=='stop-stream':
            print(await request('StopStream'))


if __name__=='__main__': asyncio.run(main())
