"""análises e histórico de decisões."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from municipal.fontes import now
from municipal.simulacao import approved_projection


# entrevista de 15/10/2026; última entrada: anotações
QUESTIONS=[
    'Quais são as principais prioridades da prefeitura para os próximos anos?',
    'O que precisaria acontecer para vocês considerarem que o projeto deu certo?',
    'Como a prefeitura decide hoje quem cobrar primeiro entre os contribuintes em atraso? Existem regras ou limites para essa cobrança?',
    'Quais são as maiores dificuldades para receber o dinheiro dos convênios e manter as obras andando?',
    'Existe uma ordem definida para realizar os pagamentos da prefeitura? Quem autoriza esses pagamentos?',
    'Agente de Coleta Externa: de quais informações externas a prefeitura depende, como repasses do governo e dados de órgãos de controle? Como vocês obtêm essas informações hoje?',
    'Agente Financeiro: quais regras e limites financeiros a prefeitura precisa respeitar no dia a dia, e quais são as maiores preocupações com o orçamento?',
    'Agente Operacional/Logístico: quais gastos do dia a dia vocês gostariam de controlar melhor? Hoje esses dados são acompanhados?',
    'Agente Revisor (com aprovação humana): quais decisões o sistema poderia tomar sozinho e quais precisariam da aprovação de uma pessoa? Quem seria essa pessoa?',
    'Escopo e entrega: quais são o prazo e o orçamento para o projeto, e quem vai usar o sistema no dia a dia?',
    'Anotações do entrevistador',
]


class Store:
    def __init__(self,path: Path):
        self.path=path
        path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,created TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,action_id TEXT NOT NULL,decision TEXT NOT NULL,reviewer TEXT NOT NULL,reason TEXT NOT NULL,created TEXT NOT NULL,senior_approver TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS project(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
            ''')
            # migração de bancos antigos
            colunas = {r['name'] for r in db.execute('PRAGMA table_info(events)')}
            if 'senior_approver' not in colunas:
                db.execute("ALTER TABLE events ADD COLUMN senior_approver TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_run(self,payload: dict) -> dict:
        item={**payload,'id':uuid.uuid4().hex,'created_at':now()}
        with self.connect() as db:
            db.execute('INSERT INTO runs VALUES(?,?,?)',(item['id'],item['created_at'],json.dumps(item,ensure_ascii=False)))
        return self.get_run(item['id'])

    def get_run(self,run_id: str) -> dict:
        with self.connect() as db:
            row=db.execute('SELECT payload FROM runs WHERE id=?',(run_id,)).fetchone()
            if not row:
                raise KeyError('Análise não encontrada.')
            events=[dict(r) for r in db.execute('SELECT * FROM events WHERE run_id=? ORDER BY seq',(run_id,))]
        item=json.loads(row['payload'])
        reviews={}
        for event in events:
            reviews[event['action_id']]=event
        item.update(reviews=reviews,audit=events)
        item['approved_projection']=approved_projection(item,reviews)
        return item

    def latest(self) -> dict | None:
        with self.connect() as db:
            row=db.execute('SELECT id FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
        return self.get_run(row['id']) if row else None

    def history(self) -> list:
        with self.connect() as db:
            rows=db.execute('SELECT id,created,payload FROM runs ORDER BY rowid DESC LIMIT 30').fetchall()
        return [dict(id=r['id'],created_at=r['created'],mode=json.loads(r['payload'])['analysis']['mode']) for r in rows]

    def review(self,run_id: str,action_id: str,decision: str,reviewer: str,reason: str,senior_approver: str='') -> dict:
        run=self.get_run(run_id)
        actions={a['id']: a for a in run['scenario']['actions']}
        if action_id not in actions:
            raise ValueError('Proposta desconhecida nesta análise.')
        if decision not in ('aprovada','rejeitada','revisar'):
            raise ValueError('Decisão inválida.')
        if not isinstance(reviewer,str) or not 2<=len(reviewer.strip())<=100:
            raise ValueError('Informe o nome do revisor (2 a 100 caracteres).')
        if not isinstance(reason,str) or not 5<=len(reason.strip())<=2000:
            raise ValueError('Registre uma justificativa (5 a 2000 caracteres).')
        senior=senior_approver.strip() if isinstance(senior_approver,str) else ''
        if actions[action_id].get('senior_approval') and decision=='aprovada' and not 2<=len(senior)<=100:
            raise ValueError('Esta proposta mexe em tributação. Informe o nome de quem a aprova em nível superior (2 a 100 caracteres).')
        with self.connect() as db:
            db.execute('INSERT INTO events(run_id,action_id,decision,reviewer,reason,created,senior_approver) VALUES(?,?,?,?,?,?,?)',
                (run_id,action_id,decision,reviewer.strip(),reason.strip(),now(),senior))
        return self.get_run(run_id)

    def project(self) -> dict:
        with self.connect() as db:
            row=db.execute('SELECT payload FROM project WHERE id=1').fetchone()
        return json.loads(row['payload']) if row else dict(consultant='',scrum_master='',team='',answers=['']*len(QUESTIONS))

    def save_project(self,data: dict) -> dict:
        if not isinstance(data,dict) or set(data)!={'consultant','scrum_master','team','answers'}:
            raise ValueError('Campos do projeto inválidos.')
        if any(not isinstance(data[k],str) or len(data[k])>2000 for k in ('consultant','scrum_master','team')):
            raise ValueError('Nomes ou equipe inválidos.')
        if not isinstance(data['answers'],list) or len(data['answers'])!=len(QUESTIONS) or any(not isinstance(a,str) or len(a)>4000 for a in data['answers']):
            raise ValueError('Respostas da entrevista inválidas.')
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO project VALUES(1,?)',(json.dumps(data,ensure_ascii=False),))
        return data
