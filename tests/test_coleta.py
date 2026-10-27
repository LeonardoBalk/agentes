"""testes da coleta no SICONFI e no IBGE, sem rede e sem espera real."""

import copy
import gzip
import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from municipal import coleta
from municipal.coleta import (COLETAS, STATUS, DataError, ler_populacao_municipio, ler_populacao_municipios,
                              ler_rgf_pessoal, nao_consultadas, resultado, valor_api, validar_populacao)

ROOT=Path(__file__).resolve().parents[1]
FIXTURES=ROOT/'tests'/'fixtures'
URL_RGF='https://apidatalake.tesouro.gov.br/ords/cdwhprd/siconfi/tt/rgf?teste'
URL_IBGE='https://servicodados.ibge.gov.br/api/v3/agregados/6579/teste'
SANTA_LEOPOLDINA=3204500
CONTRATO={'id','title','url','status','checked_at','content_sha256','note','valores','desatualizado'}


def fixture(nome: str) -> bytes:
    return (FIXTURES/nome).read_bytes()


def rgf_sem(cod_conta: str) -> bytes:
    payload=json.loads(fixture('siconfi_rgf_2025_s2.json'))
    payload['items']=[i for i in payload['items'] if i['cod_conta']!=cod_conta]
    return json.dumps(payload).encode('utf-8')


class ColetaTests(unittest.TestCase):
    def test_rgf_reads_rcl_adjusted_rcl_payroll_and_the_published_limits(self):
        r=ler_rgf_pessoal(fixture('siconfi_rgf_2025_s2.json'),URL_RGF)
        self.assertEqual(r['status'],'consultada')
        v=r['valores']
        self.assertEqual(v['rcl'],93_629_577.05)
        self.assertEqual(v['rcl_ajustada'],91_455_881.05)
        self.assertEqual(v['despesa_pessoal'],38_129_707.36)
        self.assertEqual((v['limite_maximo'],v['limite_maximo_pct_rcl_ajustada']),(49_386_175.77,54))
        self.assertEqual((v['limite_prudencial'],v['limite_prudencial_pct_rcl_ajustada']),(46_916_866.98,51.3))
        self.assertEqual((v['limite_alerta'],v['limite_alerta_pct_rcl_ajustada']),(44_447_558.19,48.6))
        self.assertEqual((v['exercicio'],v['periodo'],v['periodicidade']),(2025,2,'S'))

    def test_payroll_ratio_uses_the_adjusted_rcl_as_denominator(self):
        v=ler_rgf_pessoal(fixture('siconfi_rgf_2025_s2.json'),URL_RGF)['valores']
        self.assertAlmostEqual(v['despesa_pessoal_pct_rcl_ajustada'],41.69,delta=0.005)
        # sobre a RCL bruta daria 40,72%: o teste falha se o denominador for trocado.
        self.assertGreater(abs(v['despesa_pessoal']/v['rcl']*100-41.69),0.5)

    def test_siconfi_population_is_noted_but_not_returned_as_a_value(self):
        r=ler_rgf_pessoal(fixture('siconfi_rgf_2025_s2.json'),URL_RGF)
        self.assertNotIn('populacao',r['valores'])
        self.assertIn('15830',r['note'])
        self.assertIn('IBGE',r['note'])

    def test_empty_siconfi_responses_become_no_data_without_raising(self):
        for nome in ('siconfi_rgf_erro_periodicidade_q.json','siconfi_rgf_erro_anexo_invalido.json',
                     'siconfi_rgf_erro_sem_id_ente.json'):
            with self.subTest(nome=nome):
                r=ler_rgf_pessoal(fixture(nome),URL_RGF)
                self.assertEqual(r['status'],'sem_dados')
                self.assertEqual(r['valores'],{})

    def test_siconfi_problem_json_body_becomes_unavailable(self):
        r=ler_rgf_pessoal(fixture('siconfi_rgf_erro_texto_em_inteiro.json'),URL_RGF)
        self.assertEqual(r['status'],'indisponivel')
        self.assertIn('Bad Request',r['note'])

    def test_bytes_that_are_not_json_become_unavailable(self):
        for conteudo in (b'<html>Service Unavailable</html>',b'',b'\xff\xfe\x00'):
            with self.subTest(conteudo=conteudo):
                self.assertEqual(ler_rgf_pessoal(conteudo,URL_RGF)['status'],'indisponivel')
                self.assertEqual(ler_populacao_municipio(conteudo,URL_IBGE,SANTA_LEOPOLDINA,2025)['status'],'indisponivel')
                self.assertEqual(ler_populacao_municipios(conteudo,URL_IBGE,2025)['status'],'indisponivel')

    def test_missing_required_account_makes_the_result_unavailable_and_names_it(self):
        r=ler_rgf_pessoal(rgf_sem('LimitePrudencialDespesaComPessoalTotal'),URL_RGF)
        self.assertEqual(r['status'],'indisponivel')
        self.assertIn('LimitePrudencialDespesaComPessoalTotal',r['note'])
        self.assertEqual(r['valores'],{})

    def test_truncated_siconfi_response_is_not_read_silently(self):
        payload=json.loads(fixture('siconfi_rgf_2025_s2.json'))
        payload['hasMore']=True
        r=ler_rgf_pessoal(json.dumps(payload).encode(),URL_RGF)
        self.assertEqual(r['status'],'indisponivel')
        self.assertIn('hasMore',r['note'])

    def test_ratio_diverging_from_the_published_percentage_is_rejected(self):
        payload=json.loads(fixture('siconfi_rgf_2025_s2.json'))
        for i in payload['items']:
            if i['cod_conta']=='DespesaComPessoalTotal' and i['coluna']=='Valor':
                i['valor']=48_000_000.00
        r=ler_rgf_pessoal(json.dumps(payload).encode(),URL_RGF)
        self.assertEqual(r['status'],'indisponivel')
        self.assertIn('diverge',r['note'])

    def test_text_where_the_api_should_send_a_number_is_rejected(self):
        payload=json.loads(fixture('siconfi_rgf_2025_s2.json'))
        for i in payload['items']:
            if i['cod_conta']=='ReceitaCorrenteLiquidaAjustada':
                i['valor']='91455881.05'
        r=ler_rgf_pessoal(json.dumps(payload).encode(),URL_RGF)
        self.assertEqual(r['status'],'indisponivel')
        self.assertIn('ReceitaCorrenteLiquidaAjustada',r['note'])

    def test_ibge_population_of_the_municipality_is_an_integer(self):
        r=ler_populacao_municipio(fixture('ibge_populacao_santa_leopoldina.json'),URL_IBGE,SANTA_LEOPOLDINA,2025)
        self.assertEqual(r['status'],'consultada')
        self.assertEqual(r['valores']['populacao'],13813)
        self.assertIsInstance(r['valores']['populacao'],int)
        self.assertEqual(r['valores']['codigo_ibge'],SANTA_LEOPOLDINA)

    def test_state_list_has_78_municipalities_identified_by_code(self):
        r=ler_populacao_municipios(fixture('ibge_populacao_es.json'),URL_IBGE,2025)
        self.assertEqual(r['status'],'consultada')
        municipios=r['valores']['municipios']
        self.assertEqual(len(municipios),78)
        self.assertEqual(len({m['codigo_ibge'] for m in municipios}),78)
        santa=[m for m in municipios if m['codigo_ibge']==SANTA_LEOPOLDINA]
        self.assertEqual(len(santa),1)
        self.assertEqual(santa[0]['populacao'],13813)
        # o nome aqui é "Santa Leopoldina - ES"; na consulta individual é "Santa Leopoldina (ES)".
        self.assertNotEqual(santa[0]['nome'],'Santa Leopoldina (ES)')

    def test_ibge_period_without_data_becomes_no_data(self):
        conteudo=fixture('ibge_populacao_erro_periodo.json')
        self.assertEqual(ler_populacao_municipio(conteudo,URL_IBGE,SANTA_LEOPOLDINA,2023)['status'],'sem_dados')
        self.assertEqual(ler_populacao_municipios(conteudo,URL_IBGE,2023)['status'],'sem_dados')

    def test_year_absent_from_the_series_becomes_no_data(self):
        r=ler_populacao_municipio(fixture('ibge_populacao_santa_leopoldina.json'),URL_IBGE,SANTA_LEOPOLDINA,2021)
        self.assertEqual(r['status'],'sem_dados')

    def test_ibge_server_error_body_becomes_unavailable(self):
        conteudo=fixture('ibge_populacao_erro_municipio.json')
        self.assertEqual(ler_populacao_municipio(conteudo,URL_IBGE,SANTA_LEOPOLDINA,2025)['status'],'indisponivel')
        self.assertEqual(ler_populacao_municipios(conteudo,URL_IBGE,2025)['status'],'indisponivel')

    def test_population_validation_rejects_text_that_is_not_a_positive_count(self):
        for valor in ('treze mil','-13813','0','','13813.0',' 13813','²',13813,None):
            with self.subTest(valor=valor):
                with self.assertRaises(DataError):
                    validar_populacao(valor,'teste')
        self.assertEqual(validar_populacao('13813','teste'),13813)

    def test_one_municipality_without_value_is_dropped_and_the_list_survives(self):
        payload=json.loads(fixture('ibge_populacao_es.json'))
        serie=payload[0]['resultados'][0]['series'][5]
        del serie['serie']['2025']
        r=ler_populacao_municipios(json.dumps(payload).encode(),URL_IBGE,2025)
        self.assertEqual(r['status'],'consultada')
        self.assertEqual(len(r['valores']['municipios']),77)
        self.assertEqual(r['valores']['total_excluidos'],1)
        self.assertEqual([e['codigo'] for e in r['valores']['excluidos']],[serie['localidade']['id']])
        self.assertIn('77 municípios entraram e 1 foi excluído',r['note'])

    def test_invalid_or_repeated_municipalities_are_dropped_not_fatal(self):
        payload=json.loads(fixture('ibge_populacao_es.json'))
        series=payload[0]['resultados'][0]['series']
        series[0]['serie']['2025']='-'
        series[1]['localidade']['nivel']['id']='N3'
        series.append(copy.deepcopy(series[2]))
        r=ler_populacao_municipios(json.dumps(payload).encode(),URL_IBGE,2025)
        self.assertEqual(r['status'],'consultada')
        self.assertEqual(len(r['valores']['municipios']),75)
        self.assertEqual(r['valores']['total_excluidos'],4)
        self.assertNotIn(int(series[2]['localidade']['id']),{m['codigo_ibge'] for m in r['valores']['municipios']})

    def test_state_list_losing_more_than_ten_percent_is_unavailable(self):
        payload=json.loads(fixture('ibge_populacao_es.json'))
        for s in payload[0]['resultados'][0]['series'][:8]:
            s['serie']['2025']='...'
        r=ler_populacao_municipios(json.dumps(payload).encode(),URL_IBGE,2025)
        self.assertEqual(r['status'],'indisponivel')
        self.assertIn('8 foram excluídos',r['note'])

    def test_state_list_with_too_few_municipalities_left_is_unavailable(self):
        payload=json.loads(fixture('ibge_populacao_es.json'))
        payload[0]['resultados'][0]['series']=payload[0]['resultados'][0]['series'][:9]
        r=ler_populacao_municipios(json.dumps(payload).encode(),URL_IBGE,2025)
        self.assertEqual(r['status'],'indisponivel')

    def test_monetary_validation_rejects_instead_of_converting(self):
        for valor in ('93629577.05',True,-1,float('nan'),float('inf'),None):
            with self.subTest(valor=valor):
                with self.assertRaises(DataError):
                    valor_api(valor,'teste')
        self.assertEqual(valor_api(93_629_577.054,'teste'),93_629_577.05)

    def test_every_status_uses_the_same_result_contract(self):
        conteudo=fixture('siconfi_rgf_2025_s2.json')
        resultados=[ler_rgf_pessoal(conteudo,URL_RGF),
                    ler_rgf_pessoal(fixture('siconfi_rgf_erro_sem_id_ente.json'),URL_RGF),
                    ler_rgf_pessoal(b'nao e json',URL_RGF),
                    *nao_consultadas()]
        self.assertEqual({r['status'] for r in resultados},set(STATUS))
        for r in resultados:
            self.assertEqual(set(r),CONTRATO)
        self.assertEqual(resultados[0]['content_sha256'],hashlib.sha256(conteudo).hexdigest())
        self.assertEqual([r['id'] for r in nao_consultadas()],[c['id'] for c in COLETAS])
        self.assertTrue(all(r['checked_at'] is None for r in nao_consultadas()))
        with self.assertRaises(ValueError):
            resultado('siconfi_rgf_pessoal',URL_RGF,'ok','status fora do contrato')

    def test_readers_only_parse_bytes_and_never_open_connections(self):
        with patch('municipal.coleta.urlopen',side_effect=AssertionError('leitor não pode abrir conexão')) as rede:
            self.assertEqual(ler_rgf_pessoal(fixture('siconfi_rgf_2025_s2.json'),URL_RGF)['status'],'consultada')
            self.assertEqual(ler_populacao_municipio(fixture('ibge_populacao_santa_leopoldina.json'),URL_IBGE,
                                                     SANTA_LEOPOLDINA,2025)['status'],'consultada')
            self.assertEqual(ler_populacao_municipios(fixture('ibge_populacao_es.json'),URL_IBGE,2025)['status'],'consultada')
        rede.assert_not_called()

    def test_project_does_not_depend_on_requests(self):
        fonte=(ROOT/'municipal'/'coleta.py').read_text(encoding='utf-8')
        self.assertNotIn('import requests',fonte)
        self.assertNotIn('from requests',fonte)



class Resposta:
    """resposta HTTP falsa no formato que urlopen devolve."""
    def __init__(self,corpo: bytes,cabecalhos=None):
        self.corpo,self.status,self.headers=io.BytesIO(corpo),200,cabecalhos or {}
    def read(self,n=-1):
        return self.corpo.read(n)
    def __enter__(self):
        return self
    def __exit__(self,*args):
        return False


class Relogio:
    """relógio monotônico falso: esperar avança o tempo sem dormir."""
    def __init__(self):
        self.agora=1000.0
        self.esperas=[]
    def __call__(self):
        return self.agora
    def esperar(self,segundos):
        self.esperas.append(segundos)
        self.agora+=segundos


def erro_http(codigo: int):
    def levantar(req,timeout):
        raise HTTPError(req.full_url,codigo,'erro',{},io.BytesIO(b'{"statusCode":%d}'%codigo))
    return levantar


def responder(*respostas):
    """side_effect que devolve as respostas na ordem; cada uma é bytes, exceção ou função(req)."""
    fila=list(respostas)
    def chamada(req,timeout):
        item=fila.pop(0) if len(fila)>1 else fila[0]
        if isinstance(item,BaseException):
            raise item
        if callable(item):
            return item(req,timeout)
        return Resposta(item)
    return chamada


def extrato(ano: int,status=None) -> bytes:
    payload=json.loads(fixture(f'siconfi_extrato_entregas_{ano}.json'))
    if status:
        for i in payload['items']:
            i['status_relatorio']=status
    return json.dumps(payload).encode('utf-8')


def siconfi(extratos: dict,rgf=None,rreo=None,pares=None):
    """urlopen falso que responde pelo recurso e pelos parâmetros da URL."""
    def chamada(req,timeout):
        url=req.full_url
        if '/extrato_entregas?' in url:
            corpo=extratos[int(url.split('an_referencia=')[1][:4])]
        elif '/rreo?' in url:
            corpo=rreo
        else:
            ente=int(url.split('id_ente=')[1].split('&')[0])
            corpo=rgf if ente==SANTA_LEOPOLDINA else (pares or {}).get(ente)
        if isinstance(corpo,BaseException):
            raise corpo
        if callable(corpo):
            return corpo(req,timeout)
        return Resposta(corpo)
    return chamada


class ColetaRedeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.cache=Path(self.temp.name)/'runtime'/'coleta.json'
        self.relogio=Relogio()
        self.dia=datetime(2026,10,27,12,tzinfo=timezone.utc)
        # ritmo novo a cada teste: o intervalo não vaza de um teste para outro.
        ritmo=patch('municipal.coleta.RITMO_SICONFI',coleta.Ritmo(coleta.INTERVALO_SICONFI))
        ritmo.start()
        self.addCleanup(ritmo.stop)
        self.addCleanup(self.temp.cleanup)

    def rgf(self,**extra):
        opcoes=dict(esperar=self.relogio.esperar,relogio=self.relogio,agora=lambda:self.dia)
        opcoes.update(extra)
        return coleta.coletar_rgf_pessoal(self.cache,2025,2,**opcoes)

    def populacao(self):
        return coleta.coletar_populacao_municipio(self.cache,2025,esperar=self.relogio.esperar,agora=lambda:self.dia)

    def test_happy_path_returns_consulted_values_from_the_public_base(self):
        with patch('municipal.coleta.urlopen',side_effect=responder(fixture('siconfi_rgf_2025_s2.json'))) as rede:
            r=self.rgf()
        self.assertEqual(r['status'],'consultada')
        self.assertFalse(r['desatualizado'])
        self.assertEqual(r['valores']['rcl_ajustada'],91_455_881.05)
        pedido=rede.call_args.args[0]
        self.assertTrue(pedido.full_url.startswith('https://apidatalake.tesouro.gov.br/ords/cdwhprd/siconfi/tt/rgf?'))
        self.assertIn('co_tipo_demonstrativo=RGF%20Simplificado',pedido.full_url)
        self.assertEqual(rede.call_args.kwargs['timeout'],20)
        self.assertEqual(r['content_sha256'],hashlib.sha256(fixture('siconfi_rgf_2025_s2.json')).hexdigest())

    def test_http_500_is_tried_three_times_then_unavailable(self):
        with patch('municipal.coleta.urlopen',side_effect=erro_http(500)) as rede:
            r=self.populacao()
        self.assertEqual(rede.call_count,3)
        self.assertEqual(r['status'],'indisponivel')
        self.assertIn('3 tentativas',r['note'])
        self.assertIn('HTTP 500',r['note'])

    def test_timeout_and_connection_errors_are_tried_three_times(self):
        for erro in (TimeoutError('timed out'),URLError(ConnectionRefusedError()),URLError(OSError('dns'))):
            with self.subTest(erro=erro):
                self.relogio.esperas.clear()
                self.cache.unlink(missing_ok=True)
                with patch('municipal.coleta.urlopen',side_effect=erro) as rede:
                    r=self.populacao()
                self.assertEqual(rede.call_count,3)
                self.assertEqual(r['status'],'indisponivel')
                self.assertIn('3 tentativas',r['note'])

    def test_client_errors_are_not_retried(self):
        for codigo in (400,401,404):
            with self.subTest(codigo=codigo):
                self.relogio.esperas.clear()
                self.cache.unlink(missing_ok=True)
                with patch('municipal.coleta.urlopen',side_effect=erro_http(codigo)) as rede:
                    r=self.populacao()
                self.assertEqual(rede.call_count,1)
                self.assertEqual(self.relogio.esperas,[])
                self.assertEqual(r['status'],'indisponivel')
                self.assertIn('1 tentativa',r['note'])

    def test_http_429_is_retried_and_can_recover(self):
        limite=erro_http(429)
        with patch('municipal.coleta.urlopen',side_effect=responder(limite,fixture('ibge_populacao_santa_leopoldina.json'))) as rede:
            r=self.populacao()
        self.assertEqual(rede.call_count,2)
        self.assertEqual(r['status'],'consultada')

    def test_waits_are_five_then_fifteen_seconds(self):
        with patch('municipal.coleta.urlopen',side_effect=erro_http(503)):
            self.populacao()
        self.assertEqual(self.relogio.esperas,[5,15])

    def test_oversized_response_is_not_retried(self):
        with patch.object(coleta,'TETO_BYTES',100),\
             patch('municipal.coleta.urlopen',side_effect=responder(b'[' + b' '*200 + b']')) as rede:
            r=self.populacao()
        self.assertEqual(rede.call_count,1)
        self.assertEqual(r['status'],'indisponivel')
        self.assertIn('maior que',r['note'])

    def test_gzip_body_is_decompressed_before_reading(self):
        corpo=gzip.compress(fixture('ibge_populacao_es.json'))
        resposta=lambda req,timeout:Resposta(corpo,{'Content-Encoding':'gzip'})
        with patch('municipal.coleta.urlopen',side_effect=resposta):
            r=coleta.coletar_populacao_municipios(self.cache,2025,agora=lambda:self.dia)
        self.assertEqual(r['status'],'consultada')
        self.assertEqual(len(r['valores']['municipios']),78)

    def test_consecutive_siconfi_requests_are_at_least_one_second_apart(self):
        instantes=[]
        def registrar(req,timeout):
            instantes.append(self.relogio())
            return Resposta(fixture('siconfi_rgf_2025_s2.json'))
        inicio=time.monotonic()
        with patch('municipal.coleta.urlopen',side_effect=registrar):
            coleta.buscar_siconfi('rgf',{'a':1},self.relogio.esperar,self.relogio)
            coleta.buscar_siconfi('rgf',{'a':2},self.relogio.esperar,self.relogio)
        self.assertGreaterEqual(instantes[1]-instantes[0],1.0)
        self.assertLess(time.monotonic()-inicio,0.5)

    def test_throttle_serializes_threads(self):
        ritmo=coleta.Ritmo(0.02)
        instantes,trava=[],threading.Lock()
        def chamar():
            ritmo.aguardar()
            with trava:
                instantes.append(time.monotonic())
        threads=[threading.Thread(target=chamar) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        instantes.sort()
        self.assertTrue(all(b-a>=0.019 for a,b in zip(instantes,instantes[1:])))

    def test_ibge_requests_are_not_throttled(self):
        with patch('municipal.coleta.urlopen',side_effect=responder(fixture('ibge_populacao_santa_leopoldina.json'),
                                                                     fixture('ibge_populacao_es.json'))):
            self.populacao()
            coleta.coletar_populacao_municipios(self.cache,2025,esperar=self.relogio.esperar,agora=lambda:self.dia)
        self.assertEqual(self.relogio.esperas,[])

    def test_pagination_joins_items_from_every_page(self):
        itens=json.loads(fixture('siconfi_rgf_2025_s2.json'))['items']
        interno='https://host-5hrds-scan.prosubnet.vcndados.oraclevcn.com/ords/cdwhprd/siconfi/tt/rgf?offset=200'
        pagina1=dict(items=itens[:200],hasMore=True,limit=200,offset=0,count=200,links=[dict(rel='next',href=interno)])
        pagina2=dict(items=itens[200:],hasMore=False,limit=200,offset=200,count=len(itens)-200,links=[])
        with patch('municipal.coleta.urlopen',side_effect=responder(json.dumps(pagina1).encode(),json.dumps(pagina2).encode())) as rede:
            r=self.rgf()
        urls=[c.args[0].full_url for c in rede.call_args_list]
        self.assertEqual(len(urls),2)
        self.assertTrue(all(u.startswith(coleta.SICONFI_BASE) for u in urls))
        self.assertTrue(urls[1].endswith('&offset=200&limit=200'))
        self.assertEqual(r['status'],'consultada')
        self.assertEqual(r['valores']['despesa_pessoal'],38_129_707.36)
        self.assertEqual(self.relogio.esperas,[coleta.INTERVALO_SICONFI])

    def test_pagination_stuck_on_has_more_stops_at_the_cap(self):
        pagina=json.dumps(dict(items=[{}],hasMore=True,limit=1,offset=0,count=1)).encode()
        with patch('municipal.coleta.urlopen',side_effect=responder(pagina)) as rede:
            r=self.rgf()
        self.assertEqual(rede.call_count,coleta.TETO_PAGINAS)
        self.assertEqual(r['status'],'indisponivel')
        self.assertIn(f'depois de {coleta.TETO_PAGINAS}',r['note'])

    def test_two_calls_within_validity_make_a_single_request(self):
        with patch('municipal.coleta.urlopen',side_effect=responder(fixture('siconfi_rgf_2025_s2.json'))) as rede:
            primeiro=self.rgf()
        with patch('municipal.coleta.urlopen',side_effect=AssertionError('não deveria acessar a rede')) as rede:
            self.dia+=timedelta(days=6)
            segundo=self.rgf()
        rede.assert_not_called()
        self.assertEqual(segundo,primeiro)

    def test_expired_cache_calls_urlopen_again(self):
        with patch('municipal.coleta.urlopen',side_effect=responder(fixture('siconfi_rgf_2025_s2.json'))):
            self.rgf()
        self.dia+=coleta.VALIDADE['siconfi_rgf_pessoal']+timedelta(seconds=1)
        with patch('municipal.coleta.urlopen',side_effect=responder(fixture('siconfi_rgf_2025_s2.json'))) as rede:
            r=self.rgf()
        self.assertEqual(rede.call_count,1)
        self.assertEqual(r['checked_at'],self.dia.isoformat())

    def test_cached_value_for_another_query_is_not_reused(self):
        with patch('municipal.coleta.urlopen',side_effect=responder(fixture('siconfi_rgf_2025_s2.json'))):
            self.rgf()
        with patch('municipal.coleta.urlopen',side_effect=responder(fixture('siconfi_rgf_erro_periodicidade_q.json'))) as rede:
            r=coleta.coletar_rgf_pessoal(self.cache,2024,2,esperar=self.relogio.esperar,relogio=self.relogio,
                                         agora=lambda:self.dia)
        self.assertEqual(rede.call_count,1)
        self.assertEqual(r['status'],'sem_dados')

    def test_corrupted_or_unexpected_cache_is_treated_as_empty(self):
        for conteudo in ('isto não é json','[]','{"outra_fonte": {}}','{"ibge_populacao": {"url": 1}}'):
            with self.subTest(conteudo=conteudo):
                self.cache.parent.mkdir(parents=True,exist_ok=True)
                self.cache.write_text(conteudo,encoding='utf-8')
                with patch('municipal.coleta.urlopen',side_effect=responder(fixture('ibge_populacao_santa_leopoldina.json'))) as rede:
                    r=self.populacao()
                self.assertEqual(rede.call_count,1)
                self.assertEqual(r['status'],'consultada')
                self.assertEqual(set(coleta.ler_cache(self.cache)),{'ibge_populacao'})

    def test_failed_refresh_returns_the_last_good_value_marked_as_stale(self):
        with patch('municipal.coleta.urlopen',side_effect=responder(fixture('ibge_populacao_santa_leopoldina.json'))):
            bom=self.populacao()
        self.dia+=timedelta(days=45)
        with patch('municipal.coleta.urlopen',side_effect=erro_http(503)):
            r=self.populacao()
        self.assertEqual(r['status'],'consultada')
        self.assertTrue(r['desatualizado'])
        self.assertEqual(r['valores'],bom['valores'])
        self.assertEqual(r['checked_at'],bom['checked_at'])
        self.assertIn('há 45 dias',r['note'])
        self.assertIn('HTTP 503',r['note'])
        entrada=coleta.ler_cache(self.cache)['ibge_populacao']
        self.assertEqual(entrada['ultima_tentativa']['status'],'indisponivel')
        self.assertEqual(entrada['ultimo_bom']['valores']['populacao'],13813)

    def test_failed_refresh_without_cached_value_is_unavailable(self):
        with patch('municipal.coleta.urlopen',side_effect=erro_http(503)):
            r=self.populacao()
        self.assertEqual(r['status'],'indisponivel')
        self.assertFalse(r['desatualizado'])
        self.assertEqual(r['valores'],{})

    def test_backoff_returns_the_failure_without_calling_urlopen(self):
        with patch('municipal.coleta.urlopen',side_effect=erro_http(503)):
            self.populacao()
        self.dia+=timedelta(minutes=2)
        with patch('municipal.coleta.urlopen',side_effect=AssertionError('recuo ignorado')) as rede:
            inicio=time.monotonic()
            r=self.populacao()
        rede.assert_not_called()
        self.assertLess(time.monotonic()-inicio,0.5)
        self.assertEqual(r['status'],'indisponivel')
        self.assertIn('Nova tentativa automática em 3 min',r['note'])

    def test_backoff_serves_the_last_good_value_as_stale(self):
        with patch('municipal.coleta.urlopen',side_effect=responder(fixture('ibge_populacao_santa_leopoldina.json'))):
            self.populacao()
        self.dia+=timedelta(days=31)
        with patch('municipal.coleta.urlopen',side_effect=erro_http(503)):
            self.populacao()
        self.dia+=timedelta(minutes=1)
        with patch('municipal.coleta.urlopen',side_effect=AssertionError('recuo ignorado')) as rede:
            r=self.populacao()
        rede.assert_not_called()
        self.assertEqual(r['status'],'consultada')
        self.assertTrue(r['desatualizado'])
        self.assertIn('Nova tentativa automática em 4 min',r['note'])

    def test_after_the_backoff_the_source_is_tried_again(self):
        with patch('municipal.coleta.urlopen',side_effect=erro_http(503)):
            self.populacao()
        self.dia+=coleta.RECUO
        with patch('municipal.coleta.urlopen',side_effect=responder(fixture('ibge_populacao_santa_leopoldina.json'))) as rede:
            r=self.populacao()
        self.assertEqual(rede.call_count,1)
        self.assertEqual(r['status'],'consultada')

    def test_forcing_ignores_the_backoff(self):
        with patch('municipal.coleta.urlopen',side_effect=erro_http(503)):
            self.populacao()
        self.dia+=timedelta(minutes=1)
        with patch('municipal.coleta.urlopen',side_effect=responder(fixture('ibge_populacao_santa_leopoldina.json'))) as rede:
            r=coleta.coletar_populacao_municipio(self.cache,2025,esperar=self.relogio.esperar,agora=lambda:self.dia,
                                                 forcar=True)
        self.assertEqual(rede.call_count,1)
        self.assertEqual(r['status'],'consultada')

    def test_forcing_does_not_bypass_a_valid_cache(self):
        with patch('municipal.coleta.urlopen',side_effect=responder(fixture('ibge_populacao_santa_leopoldina.json'))):
            self.populacao()
        with patch('municipal.coleta.urlopen',side_effect=AssertionError('cache válido ignorado')) as rede:
            coleta.coletar_populacao_municipio(self.cache,2025,agora=lambda:self.dia,forcar=True)
        rede.assert_not_called()

    def periodo(self):
        return coleta.coletar_periodo_rgf(self.cache,esperar=self.relogio.esperar,relogio=self.relogio,agora=lambda:self.dia)

    def test_period_choice_is_the_latest_closed_year_homologated(self):
        extratos={a:extrato(a) for a in (2024,2025,2026)}
        with patch('municipal.coleta.urlopen',side_effect=siconfi(extratos)) as rede:
            r=self.periodo()
        self.assertEqual(rede.call_count,3)
        self.assertEqual(r['status'],'consultada')
        v=r['valores']
        self.assertEqual((v['exercicio'],v['periodo'],v['periodicidade'],v['demonstrativo']),(2025,2,'S','RGF Simplificado'))
        self.assertEqual((v['mais_recente_publicado']['exercicio'],v['mais_recente_publicado']['periodo']),(2026,1))
        self.assertEqual(v['exercicios_consultados'],[2026,2025,2024])
        self.assertIn('2025, 2º semestre',r['note'])

    def test_partial_current_year_falls_back_to_the_previous_closed_year(self):
        extratos={2026:extrato(2026),2025:erro_http(500),2024:extrato(2024)}
        with patch('municipal.coleta.urlopen',side_effect=siconfi(extratos)):
            r=self.periodo()
        self.assertEqual(r['status'],'consultada')
        self.assertEqual((r['valores']['exercicio'],r['valores']['periodo']),(2024,2))
        self.assertIn('2025:',r['note'])

    def test_rectified_report_is_not_taken_as_delivered(self):
        extratos={2026:extrato(2026),2025:extrato(2025,status='RE'),2024:extrato(2024)}
        with patch('municipal.coleta.urlopen',side_effect=siconfi(extratos)):
            r=self.periodo()
        self.assertEqual(r['valores']['exercicio'],2024)

    def test_no_homologated_period_is_no_data(self):
        extratos={a:extrato(a,status='RE') for a in (2024,2025,2026)}
        with patch('municipal.coleta.urlopen',side_effect=siconfi(extratos)):
            r=self.periodo()
        self.assertEqual(r['status'],'sem_dados')
        self.assertEqual(r['valores'],{})

    def test_every_extract_failing_is_unavailable(self):
        with patch('municipal.coleta.urlopen',side_effect=erro_http(404)):
            r=self.periodo()
        self.assertEqual(r['status'],'indisponivel')

    def test_rcl_uses_the_period_chosen_from_the_extract(self):
        extratos={a:extrato(a) for a in (2024,2025,2026)}
        with patch('municipal.coleta.urlopen',side_effect=siconfi(extratos,rgf=fixture('siconfi_rgf_2025_s2.json'))) as rede:
            r=coleta.coletar_rcl(self.cache,esperar=self.relogio.esperar,relogio=self.relogio,agora=lambda:self.dia)
        url_rgf=rede.call_args_list[-1].args[0].full_url
        self.assertIn('an_exercicio=2025&in_periodicidade=S&nr_periodo=2',url_rgf)
        self.assertEqual(r['status'],'consultada')
        self.assertEqual(r['valores']['escolha_periodo']['exercicio'],2025)
        self.assertIn('Período escolhido pelo extrato',r['note'])

    def test_rcl_without_a_period_does_not_query_the_rgf(self):
        extratos={a:extrato(a,status='RE') for a in (2024,2025,2026)}
        with patch('municipal.coleta.urlopen',side_effect=siconfi(extratos)) as rede:
            r=coleta.coletar_rcl(self.cache,esperar=self.relogio.esperar,relogio=self.relogio,agora=lambda:self.dia)
        self.assertEqual(rede.call_count,3)
        self.assertEqual(r['status'],'sem_dados')
        self.assertEqual(r['id'],'siconfi_rgf_pessoal')

    def test_cache_write_is_atomic_and_leaves_no_temporary_file(self):
        with patch('municipal.coleta.urlopen',side_effect=responder(fixture('ibge_populacao_santa_leopoldina.json'))):
            self.populacao()
        self.assertEqual([p.name for p in self.cache.parent.iterdir()],['coleta.json'])
        self.assertEqual(json.loads(self.cache.read_text(encoding='utf-8'))['ibge_populacao']['ultimo_bom']['valores']['populacao'],13813)


if __name__=='__main__':
    unittest.main()
