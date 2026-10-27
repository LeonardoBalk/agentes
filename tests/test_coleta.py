"""testes da leitura das respostas do SICONFI e do IBGE, sem rede."""

import copy
import hashlib
import json
import unittest
from pathlib import Path

from municipal.coleta import (COLETAS, STATUS, DataError, ler_populacao_municipio, ler_populacao_municipios,
                              ler_rgf_pessoal, nao_consultadas, resultado, valor_api, validar_populacao)

ROOT=Path(__file__).resolve().parents[1]
FIXTURES=ROOT/'tests'/'fixtures'
URL_RGF='https://apidatalake.tesouro.gov.br/ords/cdwhprd/siconfi/tt/rgf?teste'
URL_IBGE='https://servicodados.ibge.gov.br/api/v3/agregados/6579/teste'
SANTA_LEOPOLDINA=3204500
CONTRATO={'id','title','url','status','checked_at','content_sha256','note','valores'}


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

    def test_parsers_do_not_touch_the_network(self):
        fonte=(ROOT/'municipal'/'coleta.py').read_text(encoding='utf-8')
        for proibido in ('urlopen','urllib','http.client','socket','requests'):
            with self.subTest(proibido=proibido):
                self.assertNotIn(proibido,fonte)


if __name__=='__main__':
    unittest.main()
