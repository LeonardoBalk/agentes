"""consulta a fontes oficiais."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen


CATALOG = [
    dict(id='lrf', title='Lei de Responsabilidade Fiscal - LC 101/2000',
         url='https://www.planalto.gov.br/ccivil_03/leis/lcp/lcp101.htm',
         purpose='Renúncia de receita (art. 14), limites de despesa com pessoal (arts. 19, 20 e 22) e restos a pagar no fim do mandato (art. 42).',
         artigos=[14, 19, 20, 22, 42]),
    dict(id='lei4320', title='Lei 4.320/1964 - Normas gerais de direito financeiro',
         url='https://www.planalto.gov.br/ccivil_03/leis/l4320.htm',
         purpose='Empenho (art. 58), pagamento (art. 62), liquidação (art. 63) e ordem de pagamento (art. 64): separa despesa liquidada de despesa paga.',
         artigos=[58, 62, 63, 64]),
    dict(id='mdf', title='Manual de Demonstrativos Fiscais - Tesouro Nacional',
         url='https://www.gov.br/tesouronacional/pt-br/contabilidade-e-custos/manuais/manual-de-demonstrativos-fiscais-mdf',
         purpose='Localizar a edição aplicável e a metodologia de apuração dos demonstrativos.',
         limite='Só a página de apresentação foi lida. Os demonstrativos (RCL, despesa com pessoal) estão nos anexos do manual, que não foram lidos: consulte a edição vigente.'),
]


BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')


def decode_html(content: bytes, header_charset: str | None) -> str:
    """decodifica pelo cabeçalho, metadados ou codificação alternativa."""
    charset=header_charset
    if not charset:
        match=re.search(rb'charset=["\']?([\w-]+)',content[:4096],re.I)
        charset=match.group(1).decode('ascii') if match else None
    for candidate in (charset,'utf-8','cp1252'):
        if not candidate:
            continue
        try:
            return content.decode(candidate)
        except (LookupError,UnicodeDecodeError):
            continue
    return content.decode('cp1252',errors='replace')


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts=[]
        self.hidden=0

    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'):
            self.hidden+=1

    def handle_endtag(self,tag):
        if tag in ('script','style','noscript'):
            self.hidden=max(0,self.hidden-1)

    def handle_data(self,data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def extrair_dispositivos(texto: str, numeros: list[int], limite: int = 2500) -> list[dict]:
    """recorta os artigos solicitados."""
    achados = []
    for n in numeros:
        inicio = re.search(rf'Art\.\s*{n}\s*[º°]?\s*\.', texto)
        if not inicio:
            continue
        seguinte = re.search(rf'Art\.\s*{n + 1}\s*[º°]?\s*\.', texto[inicio.end():])
        fim = inicio.end() + seguinte.start() if seguinte else len(texto)
        achados.append(dict(dispositivo=f'Art. {n}', trecho=texto[inicio.start():fim][:limite].strip()))
    return achados


def source_status(cache: Path) -> list[dict]:
    if cache.exists():
        try:
            result=json.loads(cache.read_text(encoding='utf-8'))
            if isinstance(result,list) and {x['id'] for x in result}=={x['id'] for x in CATALOG}:
                return result
        except (ValueError,KeyError,TypeError):
            pass
    return [{**s,'status':'nao_consultada','checked_at':None,'excerpt':'',
             'note':'Referência cadastrada; conteúdo ainda não consultado.'} for s in CATALOG]


def refresh_sources(cache: Path) -> list[dict]:
    result=[]
    for s in CATALOG:
        item={**s,'checked_at':now(),'excerpt':''}
        try:
            # o site exige identificação de navegador
            req=Request(s['url'],headers={'User-Agent':BROWSER_UA,'Accept-Language':'pt-BR,pt;q=0.9'})
            with urlopen(req,timeout=20) as response:
                content=response.read(2_000_001)
                if len(content)>2_000_000:
                    raise ValueError('Documento excede o limite de leitura.')
                html=decode_html(content,response.headers.get_content_charset())
                parser=TextParser()
                parser.feed(html)
                plain=' '.join(' '.join(parser.parts).split())
                if len(plain)<200:
                    raise ValueError('Conteúdo insuficiente para consulta.')
                note='Conteúdo obtido nesta data. Confirmar vigência e aplicabilidade; a consulta não é parecer jurídico.'
                if s.get('artigos'):
                    dispositivos=extrair_dispositivos(plain,s['artigos'])
                    faltantes=[f'Art. {n}' for n in s['artigos'] if f'Art. {n}' not in {d['dispositivo'] for d in dispositivos}]
                    if not dispositivos:
                        raise ValueError('Nenhum dos artigos esperados foi encontrado.')
                    excerpt='\n\n'.join(f"{d['dispositivo']}: {d['trecho']}" for d in dispositivos)
                    item['dispositivos']=dispositivos
                    note+=f" Artigos lidos: {', '.join(d['dispositivo'] for d in dispositivos)}."
                    if faltantes:
                        note+=f" Não encontrados: {', '.join(faltantes)}."
                else:
                    pos=plain.lower().find('manual de demonstrativos')
                    excerpt=plain[max(0,pos):max(0,pos)+6000]
                    note+=' '+s.get('limite','')
                item.update(status='consultada',excerpt=excerpt,
                    content_sha256=hashlib.sha256(content).hexdigest(),
                    final_url=response.url,note=note.strip())
        except Exception as e:
            item.update(status='indisponivel',note=f'Consulta não concluída ({type(e).__name__}). Abra a referência e confira manualmente.')
        result.append(item)
    cache.parent.mkdir(parents=True,exist_ok=True)
    temp=cache.with_suffix('.tmp')
    temp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(cache)
    return result
