"""app local."""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args,**kwargs):
        return False

from municipal.agente import AgentError, ai_status, demo_analysis, run_agent
from municipal.coleta import atualizar_coleta, coleta_vazia, estado_coleta
from municipal.dados import load_dataset
from municipal.fontes import refresh_sources, source_status
from municipal.simulacao import DEFAULTS, simulate, validate_inputs
from municipal.storage import QUESTIONS, Store


ROOT=Path(__file__).resolve().parent
load_dotenv(ROOT/'.env')
os.environ.setdefault('CREWAI_TRACING_ENABLED','false')
os.environ.setdefault('OTEL_SDK_DISABLED','true')
os.environ.setdefault('CREWAI_TELEMETRY_DISABLED','true')


class Application:
    def __init__(self,root: Path=ROOT):
        self.root=root
        self.dataset=load_dataset(root/'data'/'estudo_municipal.xlsx')
        self.store=Store(root/'runtime'/'municipal.sqlite3')
        self.source_cache=root/'runtime'/'sources.json'
        self.sources_lock=threading.Lock()
        self.external_cache=root/'runtime'/'coleta.json'
        self.external_lock=threading.Lock()
        self.jobs={}
        self.job_lock=threading.Lock()

    def state(self):
        return dict(dataset=self.dataset,defaults=DEFAULTS,scenario=simulate(self.dataset),
            ai=ai_status(),sources=source_status(self.source_cache),latest=self.store.latest(),
            history=self.store.history(),project=self.store.project(),questions=QUESTIONS,external=self.external())

    def refresh(self):
        with self.sources_lock:
            return refresh_sources(self.source_cache)

    def external(self):
        # só cache: a tela inicial nunca espera rede
        return estado_coleta(self.external_cache,self.dataset['metrics']['revenue'])

    def refresh_external(self,forcar=True):
        with self.external_lock:
            return atualizar_coleta(self.external_cache,self.dataset['metrics']['revenue'],forcar=forcar)

    def start_run(self,body):
        if not isinstance(body,dict) or set(body)-{'mode','inputs'}:
            raise ValueError('Parâmetros de análise inválidos.')
        mode=body.get('mode','demo')
        if mode not in ('demo','ai'):
            raise ValueError('Modo deve ser demo ou ai.')
        inputs=validate_inputs(body.get('inputs',{}))
        if mode=='ai' and not ai_status()['ready']:
            raise AgentError('Configure o modelo e a chave no .env antes de executar com IA.')
        with self.job_lock:
            if any(j['status']=='running' for j in self.jobs.values()):
                raise ValueError('Já existe uma análise em andamento. Aguarde a conclusão.')
            job_id=uuid.uuid4().hex
            self.jobs[job_id]=dict(status='running',message='Calculando indicadores e propostas...')
        threading.Thread(target=self._run,args=(job_id,mode,inputs),daemon=True).start()
        return dict(job_id=job_id)

    def _run(self,job_id,mode,inputs):
        try:
            scenario=simulate(self.dataset,inputs)
            sources=source_status(self.source_cache)
            if mode=='ai' and not any(s['status']=='consultada' for s in sources):
                sources=self.refresh()
                if not any(s['status']=='consultada' for s in sources):
                    raise AgentError('Não foi possível obter nenhuma fonte oficial. Consulte as fontes e tente novamente. O modo demonstração está disponível.')
            external=self.external()
            if mode=='ai' and coleta_vazia(external):
                external=self.refresh_external(forcar=False)
            project=self.store.project()
            analysis=run_agent(self.dataset,scenario,sources,project) if mode=='ai' else demo_analysis(self.dataset,scenario,sources)
            run=self.store.save_run(dict(analysis=analysis,scenario=scenario,sources=sources,external=external,
                dataset=dict(filename=self.dataset['filename'],fingerprint=self.dataset['fingerprint'],
                             metrics=self.dataset['metrics'],warnings=self.dataset['warnings'],metric_sources=self.dataset['metric_sources']),
                project=project,interview_complete=all(a.strip() for a in project['answers']),
                roles_complete=all(project[k].strip() for k in ('consultant','scrum_master','team'))))
            result=dict(status='done',run=run)
        except (AgentError,ValueError) as e:
            result=dict(status='error',message=str(e))
        except Exception:
            logging.exception('Falha interna na análise')
            result=dict(status='error',message='Falha interna na análise. Veja o terminal. Nenhuma resposta foi salva como sucesso.')
        with self.job_lock:
            self.jobs[job_id]=result
            for old in list(self.jobs)[:-30]:
                if self.jobs[old]['status']!='running':
                    del self.jobs[old]


def make_handler(app: Application):
    class Handler(BaseHTTPRequestHandler):
        server_version='LeopoldinAI/1.0'

        def send(self,data,status=200,content_type='application/json; charset=utf-8',filename=None):
            raw=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Frame-Options','DENY')
            self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            if filename:
                self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(raw)

        def valid_host(self):
            return self.headers.get('Host','') in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')

        def do_GET(self):
            if not self.valid_host():
                return self.send(dict(error='Host não autorizado.'),403)
            path=urlparse(self.path).path
            try:
                assets={
                    '/':(app.root/'web'/'index.html','text/html; charset=utf-8'),
                    '/app.js':(app.root/'web'/'app.js','text/javascript; charset=utf-8'),
                    '/style.css':(app.root/'web'/'style.css','text/css; charset=utf-8'),
                    '/logo.png':(app.root/'assets'/'branding'/'logo.png','image/png'),
                    '/favicon.png':(app.root/'assets'/'branding'/'icon.png','image/png'),
                }
                if path in assets:
                    asset,mime=assets[path]
                    return self.send(asset.read_bytes(),content_type=mime)
                if path=='/api/state':
                    return self.send(app.state())
                if path=='/api/external':
                    return self.send(app.external())
                if path.startswith('/api/jobs/'):
                    with app.job_lock:
                        job=app.jobs.get(path.rsplit('/',1)[-1])
                    if not job:
                        raise KeyError('Tarefa não encontrada.')
                    return self.send(job)
                if path.startswith('/api/runs/'):
                    parts=path.strip('/').split('/')
                    run=app.store.get_run(parts[2])
                    if len(parts)==3:
                        return self.send(run)
                    if len(parts)==4 and parts[3]=='export.json':
                        return self.send(run,filename=f'analise-{run["id"][:8]}.json')
                    if len(parts)==4 and parts[3]=='export.csv':
                        buf=io.StringIO(newline='')
                        writer=csv.writer(buf,delimiter=';')
                        writer.writerow(['análise','proposta','bruto_brl','custo_brl','líquido_brl','decisão','revisor','justificativa'])
                        def safe(v):
                            text=str(v)
                            return "'"+text if text.lstrip().startswith(('=','+','-','@')) else text
                        for a in run['scenario']['actions']:
                            review=run['reviews'].get(a['id'],{})
                            writer.writerow([run['id'],a['title'],f'{a["gross"]:.2f}',f'{a["cost"]:.2f}',f'{a["net"]:.2f}',review.get('decision','pendente'),safe(review.get('reviewer','')),safe(review.get('reason',''))])
                        return self.send(('\ufeff'+buf.getvalue()).encode('utf-8'),content_type='text/csv; charset=utf-8',filename=f'propostas-{run["id"][:8]}.csv')
                return self.send(dict(error='Página não encontrada.'),404)
            except KeyError as e:
                return self.send(dict(error=str(e)),404)
            except Exception:
                logging.exception('Falha GET')
                return self.send(dict(error='Falha interna ao carregar os dados.'),500)

        def do_POST(self):
            if not self.valid_host():
                return self.send(dict(error='Host não autorizado.'),403)
            origin=self.headers.get('Origin')
            if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'):
                return self.send(dict(error='Origem não autorizada.'),403)
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                return self.send(dict(error='Envie application/json.'),415)
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=100_000:
                    raise ValueError('Tamanho de requisição inválido.')
                body=json.loads(self.rfile.read(size))
                if not isinstance(body,dict):
                    raise ValueError('Esperado objeto JSON.')
                path=urlparse(self.path).path
                if path=='/api/simulate':
                    return self.send(simulate(app.dataset,body))
                if path=='/api/runs':
                    return self.send(app.start_run(body),202)
                if path=='/api/sources/refresh':
                    return self.send(app.refresh())
                if path=='/api/external/refresh':
                    return self.send(app.refresh_external())
                if path=='/api/project':
                    return self.send(app.store.save_project(body))
                if path=='/api/reviews':
                    if not {'run_id','action_id','decision','reviewer','reason'}<=set(body)<={'run_id','action_id','decision','reviewer','reason','senior_approver'}:
                        raise ValueError('Campos de revisão inválidos.')
                    return self.send(app.store.review(**body))
                return self.send(dict(error='Rota não encontrada.'),404)
            except KeyError as e:
                return self.send(dict(error=str(e)),404)
            except (ValueError,AgentError,TypeError) as e:
                return self.send(dict(error=str(e)),400)
            except Exception:
                logging.exception('Falha POST')
                return self.send(dict(error='Falha interna. Veja o terminal.'),500)

        def log_message(self,fmt,*args):
            logging.info(fmt,*args)
    return Handler


def main():
    parser=argparse.ArgumentParser(description='LeopoldinAI - app local do estudo de caso')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--open',action='store_true')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    try:
        app=Application()
        server=ThreadingHTTPServer(('127.0.0.1',args.port),make_handler(app))
    except Exception as e:
        print(f'Não foi possível iniciar: {e}')
        raise SystemExit(1)
    url=f'http://127.0.0.1:{args.port}'
    print(f'LeopoldinAI: {url}\nModo demonstração disponível. Ctrl+C encerra o servidor.',flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nServidor encerrado.')
    finally:
        server.server_close()


if __name__=='__main__':
    main()
