"""Motor de destilacao."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
CORPUS = Path('C:\\Users\\Administrator\\Documents\\ia mind\\biblioteca-texto')
SAIDA = Path('C:\\Users\\Administrator\\Documents\\app_audit_temp\\destilacao_corpus')
URL_OR = 'https://openrouter.ai/api/v1/chat/completions'
URL_NV = 'https://integrate.api.nvidia.com/v1/chat/completions'
URL_GEMINI = 'https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s'
CAMADA_A = [('oai', 'nvidia', 'nvidia/nemotron-3-super-120b-a12b', URL_NV), ('oai', 'openrouter', 'nvidia/nemotron-3-super-120b-a12b:free', URL_OR), ('gemini', 'gemini', 'gemini-flash-lite-latest', URL_GEMINI)]
CAMADA_GEMINI = [('gemini', 'gemini', 'gemini-flash-lite-latest', URL_GEMINI), ('oai', 'nvidia', 'nvidia/nemotron-3-super-120b-a12b', URL_NV)]
CAMADA_B = [('oai', 'openrouter', 'nvidia/nemotron-3-super-120b-a12b:free', URL_OR), ('oai', 'nvidia', 'nvidia/nemotron-3-super-120b-a12b', URL_NV)]
PROVEDORES = CAMADA_A
OBRAS_NEMOTRON = []

def _gravar_atomico(destino: Path, obj: dict) -> None:
    tmp = destino.with_suffix(destino.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, destino)

def camada_da_obra(nome: str) -> str:
    return 'nemotron' if any((s.lower() in nome.lower() for s in OBRAS_NEMOTRON)) else 'gemini'
CHARS_POR_LOTE = 9000
MAX_TOKENS = 16000
PAUSA = 1.0
FLUXOS = int(os.environ.get('WORKERS', '14'))
CAMPOS = ['campo_01', 'campo_02', 'campo_03', 'campo_04', 'campo_05', 'campo_06', 'campo_07', 'campo_08', 'campo_09', 'campo_10', 'campo_11', 'campo_12', 'campo_13', 'campo_14', 'campo_15', 'campo_16', 'campo_17', 'campo_18', 'campo_19', 'campo_20']
INSTRUCAO = ''

class Anel:

    def __init__(self) -> None:
        self.chaves = {'nvidia': self._colher('NVIDIA_API_KEY'), 'openrouter': self._colher('OPENROUTER_API_KEY'), 'gemini': self._colher('GEMINI_API_KEY')}
        self.pos = {k: 0 for k in self.chaves}
        self.mortas: dict[str, set[str]] = {k: set() for k in self.chaves}
        self.mortas_rota: dict[str, set[str]] = {}
        self.usos: dict[str, int] = {}
        self._trava = threading.Lock()

    @staticmethod
    def _colher(base: str) -> list[str]:
        ks = [os.environ[base]] if os.environ.get(base) else []
        ks += [os.environ[k] for k in sorted(os.environ) if re.fullmatch(re.escape(base) + '_\\d+', k)]
        return ks

    def proxima(self, campo_02: str, rota: str='') -> str | None:
        with self._trava:
            fora = self.mortas[campo_02] | self.mortas_rota.get(rota, set())
            ks = [k for k in self.chaves[campo_02] if k not in fora]
            if not ks:
                return None
            k = ks[self.pos[campo_02] % len(ks)]
            self.pos[campo_02] += 1
            return k

    def queimar(self, campo_02: str, chave: str, rota: str='') -> None:
        with self._trava:
            if rota:
                self.mortas_rota.setdefault(rota, set()).add(chave)
            else:
                self.mortas[campo_02].add(chave)

    def contar(self, modelo: str) -> None:
        with self._trava:
            self.usos[modelo] = self.usos.get(modelo, 0) + 1

    def vivas(self, campo_02: str) -> int:
        with self._trava:
            return len(self.chaves[campo_02]) - len(self.mortas[campo_02])

def _pedir_oai(modelo: str, chave: str, texto: str, url: str) -> str:
    corpo = json.dumps({'model': modelo, 'temperature': 0, 'max_tokens': MAX_TOKENS, 'messages': [{'role': 'user', 'content': INSTRUCAO + '\n\n---TEXTO---\n' + texto}]}).encode()
    req = urllib.request.Request(url, data=corpo, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + chave})
    with urllib.request.urlopen(req, timeout=240) as r:
        d = json.load(r)
    return d['choices'][0]['message']['content']

def _pedir_gemini(modelo: str, chave: str, texto: str, url: str) -> str:
    corpo = json.dumps({'contents': [{'parts': [{'text': INSTRUCAO + '\n\n---TEXTO---\n' + texto}]}], 'generationConfig': {'temperature': 0, 'maxOutputTokens': 8192}}).encode()
    req = urllib.request.Request(url % (modelo, chave), data=corpo, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=240) as r:
        d = json.load(r)
    return d['candidates'][0]['content']['parts'][0]['text']

def para_json(txt: str) -> list[dict]:
    t = re.sub('^```(?:json)?|```$', '', txt.strip(), flags=re.M).strip()
    i, j = (t.find('['), t.rfind(']'))
    if i >= 0 and j > i:
        t = t[i:j + 1]
    saida = json.loads(t)
    if not isinstance(saida, list):
        raise ValueError("resposta nao e' array")
    fora: list[dict] = []
    for x in saida:
        if isinstance(x, dict):
            fora.append(x)
        elif isinstance(x, list):
            if len(x) != len(CAMPOS):
                raise ValueError('array posicional com %d campos, esperado %d' % (len(x), len(CAMPOS)))
            fora.append(dict(zip(CAMPOS, x)))
        else:
            raise ValueError('elemento de tipo %s' % type(x).__name__)
    return fora

def chamar(anel: Anel, texto: str, provedores=None) -> tuple[list[dict], str]:
    ultimo = 'sem tentativa'
    vazios: list[str] = []
    for rota, campo_02, modelo, url in provedores or PROVEDORES:
        for _ in range(max(1, anel.vivas(campo_02))):
            k = anel.proxima(campo_02, modelo)
            if not k:
                break
            try:
                bruto = _pedir_oai(modelo, k, texto, url) if rota == 'oai' else _pedir_gemini(modelo, k, texto, url)
                fichas = para_json(bruto)
                anel.contar(modelo)
                if not fichas:
                    vazios.append(modelo)
                    break
                return (fichas, modelo)
            except urllib.error.HTTPError as e:
                ultimo = '%s HTTP %d' % (modelo, e.code)
                if e.code == 402:
                    anel.queimar(campo_02, k, modelo)
                elif e.code in (401, 403):
                    anel.queimar(campo_02, k)
            except Exception as e:
                ultimo = '%s %s' % (modelo, type(e).__name__)
    if vazios:
        return ([], '+'.join(sorted(set(vazios))))
    raise RuntimeError(ultimo)

def lotes(texto: str) -> list[str]:
    partes, atual, n = ([], [], 0)
    for linha in texto.splitlines(keepends=True):
        if n + len(linha) > CHARS_POR_LOTE and atual:
            partes.append(''.join(atual))
            atual, n = ([], 0)
        atual.append(linha)
        n += len(linha)
    if atual:
        partes.append(''.join(atual))
    return partes

def chave_de_obra(nome: str) -> str:
    s = nome.lower()
    s = re.sub('^(vdoc\\.pub|pdfcoffee\\.com)[_-]', '', s)
    s = re.sub('\\[[^\\]]*\\]?|\\([^)]*\\)?', ' ', s)
    s = re.sub('[^a-z0-9]+', ' ', s)
    s = re.sub('\\b(pdf|free|book|edition|ed|copia|copy|translation|russo|ocr|truncado|preservado|pg|a|o|the|of|and|for|to|in|wiley|traduzido)\\b', ' ', s)
    s = re.sub('\\b\\d{1,4}\\b', ' ', s)
    return ' '.join(s.split())[:55]

def obras() -> tuple[list[Path], list[tuple[Path, Path]]]:

    def ruim(p: Path) -> int:
        return 1 if re.search('truncado|copia|copy', p.name, re.I) else 0

    def com_procedencia(p: Path) -> int:
        return 0 if any((f.name.startswith(('PROVENIENCIA', 'NOTA_')) for f in p.iterdir() if f.is_file())) else 1
    todas = []
    for d in sorted(CORPUS.iterdir()):
        f = d / 'full_text.txt' if d.is_dir() else None
        if f and f.is_file() and (f.stat().st_size >= 2048):
            todas.append(d)
    grupos: dict[str, list[Path]] = {}
    for d in todas:
        grupos.setdefault(chave_de_obra(d.name), []).append(d)
    fora, descartadas = ([], [])
    for _, membros in grupos.items():
        membros.sort(key=lambda p: (ruim(p), com_procedencia(p), -(p / 'full_text.txt').stat().st_size))
        fora.append(membros[0])
        descartadas.extend(((p, membros[0]) for p in membros[1:]))
    fora.sort(key=lambda p: p.name)
    return (fora, descartadas)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--primeiro', default='', help='substring do slug a destilar ANTES dos demais')
    ap.add_argument('--so-primeiro', action='store_true', help="destila so' o --primeiro e para")
    ap.add_argument('--limite-lotes', type=int, default=0, help='teto de lotes POR OBRA (0 = todos)')
    ap.add_argument('--so-camada', choices=['nemotron', 'gemini'], default='', help='destila apenas as obras desta camada')
    ap.add_argument('--plano', action='store_true', help="so' MOSTRA a classificacao e sai")
    ap.add_argument('--reverso', action='store_true', help='percorre a fila de TRAS PARA FRENTE (para dois trabalhadores se encontrarem no meio)')
    ap.add_argument('--motor', choices=['nemotron', 'gemini', 'openrouter'], default='', help='forca a cadeia de provedores, ignorando a camada da obra. `openrouter` bate no endpoint do OpenRouter PRIMEIRO, entao worker assim NAO disputa com os de NVIDIA')
    a = ap.parse_args()
    anel = Anel()
    if not anel.chaves['openrouter'] and (not anel.chaves['gemini']):
        print('SEM CHAVE de OpenRouter nem de Gemini no ambiente')
        return 2
    SAIDA.mkdir(parents=True, exist_ok=True)
    livros, descartadas = obras()
    if descartadas:
        print('⛔ %d copias redundantes NAO serao destiladas (economia de cota):' % len(descartadas))
        for p, vencedora in descartadas:
            print('     %-56s -> fica: %s' % (p.name[:56], vencedora.name[:44]))
        print()
    if a.primeiro:
        alvo = [d for d in livros if a.primeiro.lower() in d.name.lower()]
        resto = [d for d in livros if d not in alvo]
        livros = alvo + ([] if a.so_primeiro else resto)
    if a.so_camada:
        livros = [d for d in livros if camada_da_obra(d.name) == a.so_camada]
    if a.reverso:
        livros = list(reversed(livros))
    if a.plano:
        print('=' * 78)
        print('PLANO DE CAMADAS -- %d obras. NADA foi destilado.' % len(livros))
        print('=' * 78)
        tot = {'nemotron': [0, 0], 'gemini': [0, 0]}
        for d in livros:
            c = camada_da_obra(d.name)
            kb = (d / 'full_text.txt').stat().st_size / 1024
            tot[c][0] += 1
            tot[c][1] += kb
            print('  %-9s %7.0f KB  %s' % (c, kb, d.name[:60]))
        print('-' * 78)
        for c, (q, kb) in tot.items():
            lotes_est = kb * 1024 / CHARS_POR_LOTE
            seg = lotes_est * (32 if c == 'nemotron' else 0.7)
            print('  %-9s %2d obras | %8.0f KB | ~%5.0f lotes | ~%5.1f h' % (c, q, kb, lotes_est, seg / 3600))
        return 0
    print('=' * 78)
    print('DESTILACAO DO CORPUS -- %d obras' % len(livros))
    print('anel: nemotron (nvidia %d + or-free %d chaves) -> gemini (%d chaves)' % (anel.vivas('nvidia'), anel.vivas('openrouter'), anel.vivas('gemini')))
    print('⛔ nenhum outro provedor, por ordem do user')
    print('=' * 78, flush=True)
    for n, d in enumerate(livros, 1):
        destino = SAIDA / (d.name[:80] + '.json')
        if destino.is_file():
            print("[%2d/%d] PULA (ja' destilado)  %s" % (n, len(livros), d.name[:52]), flush=True)
            continue
        posse = SAIDA / (d.name[:80] + '.posse')
        try:
            fd = os.open(posse, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            print("[%2d/%d] OUTRO TRABALHADOR JA' PEGOU  %s" % (n, len(livros), d.name[:48]), flush=True)
            continue
        camada = a.motor or camada_da_obra(d.name)
        provedores = {'nemotron': CAMADA_A, 'gemini': CAMADA_GEMINI, 'openrouter': CAMADA_B}.get(camada, CAMADA_GEMINI)
        fluxos = 1 if camada == 'nemotron' else FLUXOS
        texto = (d / 'full_text.txt').read_text(encoding='utf-8', errors='replace')
        ls = lotes(texto)
        if a.limite_lotes:
            ls = ls[:a.limite_lotes]
        print('[%2d/%d] %-58s [%s, %d fluxo(s)]' % (n, len(livros), d.name[:58], camada, fluxos))
        print('        %.0f KB | %d lotes' % (len(texto.encode()) / 1024, len(ls)), flush=True)
        fichas, falhos = ([], [])
        t0 = time.time()
        pronto = {'n': 0}
        trava_saida = threading.Lock()

        def processar(par: tuple[int, str]) -> tuple[int, list[dict] | None, str]:
            i, lote = par
            try:
                saida, modelo = chamar(anel, lote, provedores)
                for f in saida:
                    f['_lote'] = i
                    f['_obra'] = d.name
                    f['_engine'] = modelo
                    f['passage_id'] = hashlib.sha256((d.name + str(i) + str(f.get('campo_20', ''))).encode()).hexdigest()[:16]
                return (i, saida, '')
            except Exception as exc:
                return (i, None, str(exc)[:200])
        parcial = SAIDA / (d.name[:80] + '.parcial.json')
        ja_feitos: set[int] = set()
        if parcial.is_file():
            try:
                prev = json.loads(parcial.read_text(encoding='utf-8'))
                fichas = prev.get('fichas', [])
                falhos = prev.get('falhos', [])
                ja_feitos = set(prev.get('lotes_feitos', []))
                pronto['n'] = len(ja_feitos)
                print("        RETOMANDO: %d lotes ja' feitos, %d fichas em disco" % (len(ja_feitos), len(fichas)), flush=True)
            except Exception as exc:
                print('        ⚠️  parcial ilegivel (%s) -- recomeca a obra' % exc, flush=True)
                fichas, falhos, ja_feitos = ([], [], set())
        pendentes = [(i, lote) for i, lote in enumerate(ls) if i not in ja_feitos]
        desde_ckpt = {'n': 0}
        with ThreadPoolExecutor(max_workers=fluxos) as pool:
            for i, saida, erro in pool.map(processar, pendentes):
                with trava_saida:
                    if saida is None:
                        falhos.append({'lote': i, 'erro': erro})
                    else:
                        fichas.extend(saida)
                    ja_feitos.add(i)
                    pronto['n'] += 1
                    desde_ckpt['n'] += 1
                    if desde_ckpt['n'] >= 10 or pronto['n'] == len(ls):
                        _gravar_atomico(parcial, {'obra': d.name, 'PARCIAL': True, 'fichas': fichas, 'falhos': falhos, 'lotes_feitos': sorted(ja_feitos), 'de_um_total_de': len(ls)})
                        desde_ckpt['n'] = 0
                    if pronto['n'] % 10 == 0 or pronto['n'] == len(ls):
                        print('        lote %4d/%d | fichas %4d | falhos %3d | %.1f min' % (pronto['n'], len(ls), len(fichas), len(falhos), (time.time() - t0) / 60), flush=True)
        fichas.sort(key=lambda f: (f.get('_lote', 0), str(f.get('campo_01', ''))))
        destino.write_text(json.dumps({'obra': d.name, 'quando_utc': datetime.now(timezone.utc).isoformat(), 'chars_fonte': len(texto), 'lotes': len(ls), 'lotes_falhos': falhos, 'modelos_usados': dict(anel.usos), 'esquema': 'ESQUEMA_DESTILACAO_ESPECIFICACAO_DE_ESTRATEGIA_20260816', 'AVISO': "Ficha destilada por modelo de linguagem. `campo_20` e' o unico campo conferivel contra a fonte -- confira-o antes de usar qualquer outro campo como fundamentacao.", 'fichas': fichas}, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
        try:
            parcial.unlink(missing_ok=True)
        except Exception:
            pass
        print('        -> %d fichas, %d lotes falhos | %s' % (len(fichas), len(falhos), destino.name), flush=True)
        if a.so_primeiro:
            break
    print('-' * 78)
    print('modelos usados:', anel.usos)
    print('chaves vivas: openrouter %d | gemini %d' % (anel.vivas('openrouter'), anel.vivas('gemini')))
    print('saida:', SAIDA)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
