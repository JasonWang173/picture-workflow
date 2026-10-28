"""HTTP benchmark against a disposable checkout, never the live website."""
import argparse
import hashlib
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from benchmark_composition import fixtures


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--port', type=int, default=18082)
    args=parser.parse_args()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',args.port))
    root=Path(args.root)
    fixtures(root)
    env=os.environ.copy()
    env['BATCH_WORKERS']='2'
    log=open(root/'server.log','w')
    process=subprocess.Popen([sys.executable,'-m','uvicorn','server:app','--host','127.0.0.1','--port',str(args.port)], cwd=args.source, env=env, stdout=log,stderr=log)
    base='http://127.0.0.1:'+str(args.port)
    results={}
    try:
        for _ in range(80):
            if process.poll() is not None: raise RuntimeError('Service exited; see '+str(root/'server.log'))
            try:
                requests.get(base+'/api/health',timeout=1).raise_for_status()
                break
            except requests.RequestException: time.sleep(.1)
        else: raise RuntimeError('Service did not start')
        cases=[('overlay-jpg','overlay',['flat.jpg']), ('cutout-slot','cutout',['slot.png']), ('auto-alpha','auto',['alpha.png']), ('auto-slot','auto',['slot.png']), ('template-replace','template_replace',['slot.png','slot.png']), ('mixed','auto',['alpha.png','slot.png'])]
        for label,mode,templates in cases:
            files=[('templates',(str(i)+name,(root/name).read_bytes(),'image/png')) for i,name in enumerate(templates)]
            if mode!='template_replace':
                files += [('products',('product'+str(i)+'.jpg',(root/'product.jpg').read_bytes(),'image/jpeg')) for i in range(4)]
            start=time.perf_counter()
            response=requests.post(base+'/api/compose-batch',files=files,data={'mode':mode,'size':'1080x1920'},timeout=20)
            response.raise_for_status()
            status_url=base+response.json()['status_url']
            for _ in range(1200):
                status=requests.get(status_url,timeout=5).json()
                if status['status'] in ('done','failed'): break
                time.sleep(.05)
            assert status['status']=='done' and not status['skipped'],status
            expected=[(t,p) for t in range(1,len(templates)+1) for p in range(1, 2 if mode=='template_replace' else 5)]
            assert [(x['template_index'],x['product_index']) for x in status['images']]==expected,status
            elapsed=round(time.perf_counter()-start,3)
            hashes=[hashlib.sha256(requests.get(base+image['url'],timeout=10).content).hexdigest() for image in status['images']]
            results[label]={'seconds':elapsed,'hashes':hashes}
            print(label,elapsed,'seconds',len(hashes),'images',flush=True)
        # A single heavy request must leave health/status endpoints responsive.
        def single():
            response=requests.post(base+'/api/compose', files={'template':('flat.jpg',(root/'flat.jpg').read_bytes(),'image/jpeg'),'product':('product.jpg',(root/'product.jpg').read_bytes(),'image/jpeg')}, data={'mode':'overlay','size':'1080x1920'},timeout=30)
            response.raise_for_status()
            return hashlib.sha256(response.content).hexdigest()
        with ThreadPoolExecutor(max_workers=1) as caller:
            future=caller.submit(single)
            time.sleep(.1)
            start=time.perf_counter()
            requests.get(base+'/api/health',timeout=20).raise_for_status()
            results['single-health-latency']=round(time.perf_counter()-start,3)
            results['single-sha256']=future.result()
        print('health-during-single',results['single-health-latency'],flush=True)
        (root/'http-results.json').write_text(json.dumps(results,indent=2))
    finally:
        process.terminate()
        try: process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
        log.close()


if __name__=='__main__': main()
