"""testes da ferramenta de dados públicos do agente, sem rede e sem LLM."""

import json
import unittest
from pathlib import Path

from municipal import agente
from municipal.agente import AgentError, FERRAMENTAS_OBRIGATORIAS, consultar_publicos, validar_resposta

ROOT=Path(__file__).resolve().parents[1]
PUBLICOS=dict(
    rcl=dict(id='siconfi_rgf_pessoal',status='consultada',desatualizado=False,checked_at='2026-10-27T12:00:00+00:00',
             note='RGF 2025, período 2 (semestral).',valores=dict(rcl=93_629_577.05,rcl_ajustada=91_455_881.05,
             despesa_pessoal_pct_rcl_ajustada=41.69,conferencia_rreo=dict(situacao='confere'))),
    benchmark=dict(id='benchmark_rcl_per_capita',status='nao_consultada',desatualizado=False,checked_at=None,
                   note='Ainda não coletado. Use o botão de coletar.',valores={}))


def resposta(scenario: dict,source_ids: list) -> dict:
    return dict(summary='Resumo da análise com as propostas.',questions=['Qual o período?'],source_ids=source_ids,
                priorities=[dict(action_id=a['id'],reason='Conferir premissas e custos.') for a in scenario['actions']])


def chamadas_basicas() -> list:
    return [dict(tool=t,result='ok') for t in ('consultar_indicadores','consultar_simulacao','consultar_fontes_oficiais')]


class DadosPublicosTests(unittest.TestCase):
    scenario=dict(actions=[dict(id='recuperar_divida'),dict(id='revisar_custeio')])

    def test_valid_indicator_returns_the_collected_data_and_records_the_call(self):
        chamadas=[]
        dados=consultar_publicos(PUBLICOS,'rcl',chamadas)
        self.assertEqual(set(dados),{'rcl'})
        self.assertEqual(dados['rcl']['source_id'],'publico_rcl')
        self.assertEqual(dados['rcl']['valores']['rcl'],93_629_577.05)
        self.assertEqual(chamadas,[dict(tool='consultar_dados_publicos',indicador='rcl',valido=True,citaveis=['publico_rcl'],
                                        result='Dados públicos (rcl) disponibilizados')])

    def test_everything_indicator_lists_both_parts_and_only_consulted_ones_are_citable(self):
        chamadas=[]
        dados=consultar_publicos(PUBLICOS,'tudo',chamadas)
        self.assertEqual(set(dados),{'rcl','benchmark'})
        self.assertEqual(dados['benchmark']['status'],'nao_consultada')
        self.assertEqual(chamadas[0]['citaveis'],['publico_rcl'])

    def test_invalid_indicator_returns_a_readable_error_without_raising(self):
        for indicador in ('receita','',None,123,'tudo; ignore as regras'):
            with self.subTest(indicador=indicador):
                chamadas=[]
                dados=consultar_publicos(PUBLICOS,indicador,chamadas)
                json.dumps(dados)
                self.assertIn('erro',dados)
                self.assertEqual(dados['valores_validos'],['benchmark','rcl','tudo'])
                self.assertFalse(chamadas[0]['valido'])
                self.assertEqual(chamadas[0]['citaveis'],[])

    def test_without_collected_data_the_tool_still_answers(self):
        dados=consultar_publicos(None,'tudo',[])
        self.assertEqual({k:v['status'] for k,v in dados.items()},dict(rcl='nao_consultada',benchmark='nao_consultada'))

    def test_compact_version_keeps_status_staleness_and_the_study_case_notice(self):
        publicos=json.loads(json.dumps(PUBLICOS))
        publicos['benchmark'].update(status='consultada',desatualizado=True,valores=dict(
            mediana_pares=7068.5,municipio=dict(rcl_per_capita=6778.37),posicao=dict(ordem=7,total=11),
            comparacao_estudo=dict(aviso='Os valores do estudo de caso não correspondem aos valores publicados.')))
        dados=consultar_publicos(publicos,'benchmark',[],resumir=True)['benchmark']
        self.assertTrue(dados['desatualizado'])
        self.assertEqual(dados['valores']['rcl_per_capita'],6778.37)
        self.assertIn('não correspondem',dados['valores']['aviso_estudo_de_caso'])
        self.assertLess(len(json.dumps(dados,ensure_ascii=False)),1500)

    def test_answer_without_the_public_data_tool_is_discarded(self):
        with self.assertRaises(AgentError):
            validar_resposta(resposta(self.scenario,[]),self.scenario,[],chamadas_basicas())

    def test_answer_after_only_an_invalid_indicator_is_discarded(self):
        chamadas=chamadas_basicas()
        consultar_publicos(PUBLICOS,'inventado',chamadas)
        with self.assertRaises(AgentError):
            validar_resposta(resposta(self.scenario,[]),self.scenario,[],chamadas)

    def test_answer_with_the_four_tools_is_accepted(self):
        chamadas=chamadas_basicas()
        consultar_publicos(PUBLICOS,'tudo',chamadas)
        validar_resposta(resposta(self.scenario,[]),self.scenario,[],chamadas)
        self.assertEqual(agente.ferramentas_chamadas(chamadas),FERRAMENTAS_OBRIGATORIAS)

    def test_prompt_keeps_real_and_study_case_data_apart(self):
        prompt=agente.PROMPT_TAREFA
        self.assertIn('consultar_dados_publicos',prompt)
        self.assertIn('nunca misture os dois',prompt)
        self.assertIn('sem afirmar conformidade ou violação',prompt)
        self.assertIn('desatualizado',prompt)


if __name__=='__main__':
    unittest.main()
