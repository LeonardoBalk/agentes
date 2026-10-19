"""integração com banco temporário."""

import json
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from main import Application, make_handler

ROOT=Path(__file__).resolve().parents[1]


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        root=Path(cls.temp.name)
        (root/'data').mkdir()
        shutil.copyfile(ROOT/'data'/'estudo_municipal.xlsx',root/'data'/'estudo_municipal.xlsx')
        cls.app=Application(root)
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(cls.app))
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.temp.cleanup()

    def request(self,path,body=None,headers=None):
        data=json.dumps(body).encode() if body is not None else None
        req=Request(self.url+path,data=data,headers=headers or {'Content-Type':'application/json'})
        with urlopen(req,timeout=15) as r:
            return json.load(r)

    def test_demo_run_review_exports_and_isolation(self):
        result=self.request('/api/runs',{'mode':'demo','inputs':{}})
        for _ in range(50):
            job=self.request('/api/jobs/'+result['job_id'])
            if job['status']!='running':
                break
            time.sleep(.05)
        self.assertEqual(job['status'],'done')
        run=job['run']
        self.assertEqual(run['analysis']['mode'],'demo')
        self.assertFalse(run['interview_complete'])
        updated=self.request('/api/reviews',dict(run_id=run['id'],action_id='recuperar_divida',decision='rejeitada',reviewer='Teste HTTP',reason='Prazo nao confirmado.'))
        self.assertEqual(updated['reviews']['recuperar_divida']['decision'],'rejeitada')
        exported=self.request('/api/runs/'+run['id']+'/export.json')
        self.assertEqual(len(exported['audit']),1)
        with urlopen(self.url+'/api/runs/'+run['id']+'/export.csv') as r:
            text=r.read().decode('utf-8-sig')
        self.assertIn('rejeitada',text)
        self.assertIn('Teste HTTP',text)

    def test_reject_cross_origin_and_invalid_numbers(self):
        with self.assertRaises(HTTPError) as ctx:
            self.request('/api/simulate',{'admin_pct':-10})
        self.assertEqual(ctx.exception.code,400)
        with self.assertRaises(HTTPError) as ctx:
            self.request('/api/project',{},headers={'Content-Type':'application/json','Origin':'https://example.org'})
        self.assertEqual(ctx.exception.code,403)

    def test_reject_unknown_modes_and_malformed_requests(self):
        for body in [[],{'mode':'inventado'},{'mode':'demo','inputs':{'recovery_pct':None}}]:
            with self.subTest(body=body),self.assertRaises(HTTPError) as ctx:
                self.request('/api/runs',body)
            self.assertEqual(ctx.exception.code,400)

    def test_equilibrium_candidate_can_be_loaded_without_approving_anything(self):
        state=self.request('/api/state')
        self.assertFalse(state['scenario']['objective']['meets'])
        e=state['scenario']['equilibrium']
        self.assertEqual(e['status'],'candidate')
        result=self.request('/api/simulate',e['inputs'])
        self.assertTrue(result['objective']['meets'])
        self.assertEqual(result['equilibrium']['status'],'covered')
        result=self.request('/api/runs',{'mode':'demo','inputs':e['inputs']})
        for _ in range(50):
            job=self.request('/api/jobs/'+result['job_id'])
            if job['status']!='running':
                break
            time.sleep(.05)
        self.assertEqual(job['status'],'done')
        exported=self.request('/api/runs/'+job['run']['id']+'/export.json')
        self.assertTrue(exported['scenario']['objective']['meets'])
        self.assertEqual(exported['reviews'],{})
        self.assertFalse(exported['approved_projection']['objective']['meets'])


if __name__=='__main__':
    unittest.main()
