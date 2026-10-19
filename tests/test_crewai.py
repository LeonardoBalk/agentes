"""integração com provedor local, sem custo externo."""

import importlib.util
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from municipal.agente import AgentError, run_agent
from municipal.dados import load_dataset
from municipal.simulacao import simulate


@unittest.skipUnless(importlib.util.find_spec('crewai'),'CrewAI opcional nao instalado')
class CrewAITests(unittest.TestCase):
    def test_real_crewai_calls_all_tools_and_returns_validated_output(self):
        requests=[]
        class Provider(BaseHTTPRequestHandler):
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(payload)
                messages=payload.get('messages',[])
                if not any(m.get('role')=='tool' for m in messages) and payload.get('tools'):
                    calls=[dict(id=f'call_{i}',type='function',function=dict(name=t['function']['name'],arguments='{}'))
                           for i,t in enumerate(payload['tools']) if t['function']['name'] in ('consultar_indicadores','consultar_simulacao','consultar_fontes_oficiais')]
                    message=dict(role='assistant',content=None,tool_calls=calls)
                    finish='tool_calls'
                else:
                    diagnosis=dict(summary='Analise controlada de integracao. As bases precisam de revisao humana.',
                        priorities=[dict(action_id=i,reason='Conferir premissas, custos e prazo com o cliente.')
                                    for i in ('recuperar_divida','revisar_custeio','reduzir_folha','refis_iptu','ordenar_pagamentos')],
                        questions=['Qual o periodo exato das despesas?'],source_ids=['lrf'])
                    message=dict(role='assistant',content=json.dumps(diagnosis))
                    finish='stop'
                result=dict(id='chatcmpl-local-test',object='chat.completion',created=1,model='gpt-4o-mini',
                    choices=[dict(index=0,message=message,finish_reason=finish)],
                    usage=dict(prompt_tokens=10,completion_tokens=10,total_tokens=20))
                raw=json.dumps(result).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def log_message(self,*args):
                pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        root=Path(__file__).resolve().parents[1]
        dataset=load_dataset(root/'data'/'estudo_municipal.xlsx')
        source=[dict(id='lrf',status='consultada',excerpt='Documento de teste controlado, nao consulta externa.',checked_at='2026-10-15T00:00:00Z')]
        env=dict(LLM_MODEL='openai/gpt-4o-mini',LLM_API_KEY='local-test-not-a-secret',
            LLM_BASE_URL=f'http://127.0.0.1:{server.server_port}/v1',
            CREWAI_TRACING_ENABLED='false',OTEL_SDK_DISABLED='true',CREWAI_TELEMETRY_DISABLED='true')
        try:
            project=dict(answers=['Periodo confirmado pelo cliente: 12 meses.']+['']*10,consultant='NOME_DA_EQUIPE_NAO_ENVIAR')
            with patch.dict(os.environ,env):
                result=run_agent(dataset,simulate(dataset),source,project)
            self.assertEqual(result['mode'],'ai')
            self.assertEqual({c['tool'] for c in result['tool_log']},{'consultar_indicadores','consultar_simulacao','consultar_fontes_oficiais'})
            self.assertEqual(result['source_ids'],['lrf'])
            self.assertGreaterEqual(len(requests),2)
            self.assertIn('Periodo confirmado pelo cliente',json.dumps(requests))
            self.assertNotIn('NOME_DA_EQUIPE_NAO_ENVIAR',json.dumps(requests))
        finally:
            server.shutdown();server.server_close()


if __name__=='__main__':
    unittest.main()
