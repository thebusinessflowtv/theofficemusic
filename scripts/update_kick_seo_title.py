#!/usr/bin/env python3
import base64,json,os,time,urllib.error,urllib.request

REPO=os.environ['GITHUB_REPOSITORY']; TOKEN=os.environ['GH_TOKEN']; KICK_TOKEN=os.environ.get('KICK_ACCESS_TOKEN','').strip()
SESSION_ID='4c336705-e45f-4b1c-868f-72eff5849850'
TITLE='deep house radio 💻 music to work/study/focus to | Peter Lofi'
API=f'https://api.github.com/repos/{REPO}'


def gh(method,path,body=None,allow_404=False):
    headers={'Authorization':f'Bearer {TOKEN}','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'Peter-Lofi-Kick-SEO'}
    data=None
    if body is not None:
        headers['Content-Type']='application/json'; data=json.dumps(body).encode()
    req=urllib.request.Request(f'{API}/{path.lstrip("/")}',data=data,headers=headers,method=method)
    try:
        with urllib.request.urlopen(req,timeout=30) as r:
            raw=r.read(); return json.loads(raw.decode()) if raw else {}
    except urllib.error.HTTPError as e:
        if allow_404 and e.code==404:return None
        raise


def update_json(path,mutator,message):
    for attempt in range(1,8):
        obj=gh('GET',f'contents/{path}',allow_404=True)
        if not obj:return
        data=json.loads(base64.b64decode((obj.get('content') or '').replace('\n','')).decode())
        mutator(data)
        body={'message':message,'branch':'main','sha':obj['sha'],'content':base64.b64encode((json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode()).decode()}
        try:
            gh('PUT',f'contents/{path}',body); return
        except Exception:
            time.sleep(attempt)
    raise RuntimeError(f'Could not update {path}')


def main():
    live_api_updated=False
    api_error=None
    if KICK_TOKEN:
        try:
            req=urllib.request.Request('https://api.kick.com/public/v1/channels',data=json.dumps({'stream_title':TITLE}).encode(),headers={'Authorization':f'Bearer {KICK_TOKEN}','Content-Type':'application/json','User-Agent':'Peter-Lofi-Kick-SEO'},method='PATCH')
            with urllib.request.urlopen(req,timeout=30):pass
            live_api_updated=True
            print('Updated live Kick title:',TITLE)
        except urllib.error.HTTPError as exc:
            api_error=f'HTTP {exc.code}'
            print(f'Kick API title update unavailable ({api_error}); persisting desired title in repo state.')
        except Exception as exc:
            api_error=str(exc)
            print(f'Kick API title update unavailable ({api_error}); persisting desired title in repo state.')
    else:
        api_error='missing_access_token'
        print('No Kick access token; repo state will still be updated.')
    stamp=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    update_json(f'control/kick-live-queue/{SESSION_ID}.json',lambda d:d.update({'title':TITLE,'seo_title_updated_at':stamp,'kick_title_api_updated':live_api_updated,'kick_title_api_error':api_error}),f'seo: update Kick live title {SESSION_ID}')
    update_json(f'control/kick-live-results/{SESSION_ID}.json',lambda d:d.update({'desired_title':TITLE,'kick_title_api_updated':live_api_updated,'kick_title_api_error':api_error}),f'seo: sync Kick desired live title {SESSION_ID}')

if __name__=='__main__':main()
