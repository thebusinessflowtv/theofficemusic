import json, os
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.parse import urlencode
ROOT = Path('control/gaming-twitch-dj-30')
def append(library, track):
    playlist = next((p for p in library['playlists'] if p['key'] == 'gaming-radio'), None)
    if playlist is None: raise RuntimeError('Gaming playlist missing')
    tracks = playlist.setdefault('tracks', [])
    if not any(t['id'] == track['id'] for t in tracks): tracks.append(dict(track, position=len(tracks)+1))
    playlist['track_count'] = len(tracks)
    playlist['total_duration_seconds'] = sum(t.get('duration_seconds', 0) for t in tracks)
    library['updated_at'] = datetime.now(timezone.utc).isoformat()
    return library
if __name__ == '__main__':
    index = int(os.environ['INDEX'])
    item = json.loads((ROOT/'queue.json').read_text())['tracks'][index-1]
    qc = json.loads(Path('build/qc.json').read_text())
    if not qc.get('approved'): raise SystemExit('QC not approved')
    url = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/releases/download/{os.environ['RELEASE_TAG']}/{os.environ['MP3_NAME']}"
    track = {'id': item['id'], 'title': item['title'], 'url': url, 'duration_seconds': 300, 'source': 'gaming-twitch-dj-30', 'quality_gate': 'technical_checks_passed'}
    p = Path('control/music-library.json')
    p.write_text(json.dumps(append(json.loads(p.read_text()), track), indent=2)+'\n')
    p = Path('ovh-streaming/stations/gaming.json'); station = json.loads(p.read_text())
    if not any(t['id'] == track['id'] for t in station['tracks']): station['tracks'].append(dict(track, position=len(station['tracks'])+1))
    p.write_text(json.dumps(station, indent=2)+'\n')
    delivery = 'pending_local_sync'
    token = os.environ.get('OVH_AGENT_TOKEN', '')
    if token:
        base = 'https://peterlofi.odsgn.com.br/api/ovh/agent/runtime-config'
        headers = {'x-ovh-agent-token': token, 'content-type': 'application/json'}
        try:
            query = urlencode({'path': 'control/music-library.json', 'raw': '1'})
            with urlopen(Request(base+'?'+query, headers=headers), timeout=60) as r: library = json.load(r)
            payload = {'path': 'control/music-library.json', 'payload': append(library, track)}
            with urlopen(Request(base, data=json.dumps(payload).encode(), headers=headers, method='POST'), timeout=60) as r:
                if not json.load(r).get('ok'): raise RuntimeError('OVH did not acknowledge')
            delivery = 'added_to_ovh_gaming'
        except Exception as exc:
            delivery = 'pending_ovh_retry'
            print('OVH delivery pending:', type(exc).__name__)
    result = {'index':index,'status':'generated','track':track,'delivery':delivery,'quality_gate':qc,'run_id':os.environ['GITHUB_RUN_ID']}
    (ROOT/f'{index:02d}.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'index':index,'title':item['title'],'delivery':delivery}))
