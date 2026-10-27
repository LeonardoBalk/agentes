"""integração com banco temporário."""

import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch
from urllib.request import Request, urlopen

from main import Application, make_handler
from municipal import coleta

ROOT=Path(__file__).resolve().parents[1]
FIXTURES=ROOT/'tests'/'fixtures'
VAZIO=b'{"items":[],"hasMore":false,"count":0}'


class Resposta(io.BytesIO):
    status,headers=200,{}
    def __enter__(self):
        return self
    def __exit__(self,*args):
        return False


def apis_publicas(req,timeout):
    """SICONFI e IBGE falsos a partir dos fixtures; todo par responde com o RGF de Santa Leopoldina."""
    url=req.full_url
    if url.startswith('https://servicodados.ibge.gov.br/'):
        nome='ibge_populacao_es.json'
    elif '/extrato_entregas?' in url:
        nome=f"siconfi_extrato_entregas_{url.split('an_referencia=')[1][:4]}.json"
    elif '/rreo?' in url:
        nome='siconfi_rreo_2025_b6_anexo03.json'
    else:
        nome='siconfi_rgf_2025_s2.json'
    caminho=FIXTURES/nome
    return Resposta(caminho.read_bytes() if caminho.exists() else VAZIO)


def coletar_sem_esperar(*args,**kwargs):
    return coleta.atualizar_coleta(*args,esperar=lambda segundos:None,relogio=lambda:0.0,**kwargs)


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

    def test_state_and_external_never_touch_the_network(self):
        self.app.external_cache.unlink(missing_ok=True)
        sem_rede=AssertionError('a tela inicial não pode esperar rede')
        with patch('municipal.coleta.urlopen',side_effect=sem_rede) as coleta_rede,\
             patch('municipal.fontes.urlopen',side_effect=sem_rede) as fontes_rede:
            inicio=time.monotonic()
            state=self.request('/api/state')
            external=self.request('/api/external')
        coleta_rede.assert_not_called()
        fontes_rede.assert_not_called()
        self.assertLess(time.monotonic()-inicio,2)
        self.assertEqual(state['external'],external)
        self.assertEqual({k:v['status'] for k,v in external.items()},dict(rcl='nao_consultada',benchmark='nao_consultada'))

    def test_external_refresh_fills_the_cache_and_later_reads_need_no_network(self):
        self.app.external_cache.unlink(missing_ok=True)
        with patch('main.atualizar_coleta',side_effect=coletar_sem_esperar) as atualizar,\
             patch('municipal.coleta.RITMO_SICONFI',coleta.Ritmo(0)),\
             patch('municipal.coleta.urlopen',side_effect=apis_publicas):
            coletado=self.request('/api/external/refresh',{})
        self.assertTrue(atualizar.call_args.kwargs['forcar'])
        self.assertEqual(coletado['rcl']['status'],'consultada')
        self.assertEqual(coletado['rcl']['valores']['rcl'],93_629_577.05)
        self.assertEqual(coletado['benchmark']['status'],'consultada')
        self.assertIn('não correspondem aos valores publicados',coletado['benchmark']['note'])
        with patch('municipal.coleta.urlopen',side_effect=AssertionError('não deveria acessar a rede')) as rede:
            external=self.request('/api/external')
            state=self.request('/api/state')
        rede.assert_not_called()
        self.assertEqual(external,coletado)
        self.assertEqual(state['external']['rcl']['status'],'consultada')

    def test_demo_mode_runs_without_network_or_llm_key(self):
        sem_rede=AssertionError('o modo demonstração não usa rede')
        sem_chave={k:v for k,v in os.environ.items() if k not in ('LLM_API_KEY','GROQ_API_KEY')}
        with patch.dict(os.environ,sem_chave,clear=True),\
             patch('municipal.coleta.urlopen',side_effect=sem_rede) as coleta_rede,\
             patch('municipal.fontes.urlopen',side_effect=sem_rede) as fontes_rede:
            self.assertFalse(self.request('/api/state')['ai']['ready'])
            result=self.request('/api/runs',{'mode':'demo','inputs':{}})
            for _ in range(50):
                job=self.request('/api/jobs/'+result['job_id'])
                if job['status']!='running':
                    break
                time.sleep(.05)
        coleta_rede.assert_not_called()
        fontes_rede.assert_not_called()
        self.assertEqual(job['status'],'done')
        self.assertEqual(job['run']['analysis']['mode'],'demo')
        self.assertIn('external',job['run'])


if __name__=='__main__':
    unittest.main()
