"""Motor de destilacao."""
from __future__ import annotations
import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
AQUI = Path(__file__).resolve().parent
CORPUS = AQUI / 'corpus'
FICHAS = AQUI / 'fichas'
FALHAS = FICHAS / '_falhas'
sys.path.insert(0, str(AQUI))
import engine as da

def _e_paga(rota) -> bool:
    _, familia, modelo, _ = rota
    return familia == 'openrouter' and (not modelo.endswith(':free'))
ESQUEMA = 'FICHA_DE_SAIDA_DE_IA_20260914'
INSTRUCAO = ''

def obras() -> list[Path]:
    if not CORPUS.is_dir():
        return []
    return sorted((p for p in CORPUS.glob('*.txt') if p.name != 'full_text.txt'))

def _processo_vivo(pid: int):
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 4096
        STILL_ACTIVE = 259
        k = ctypes.windll.kernel32
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            erro = k.GetLastError()
            return False if erro == 87 else True if erro == 5 else None
        try:
            codigo = ctypes.c_ulong()
            if not k.GetExitCodeProcess(h, ctypes.byref(codigo)):
                return None
            return codigo.value == STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None

def _posse_orfa(posse: Path) -> bool:
    try:
        cru = posse.read_text(encoding='utf-8').strip()
    except OSError:
        return False
    if not cru.isdigit():
        return False
    return _processo_vivo(int(cru)) is False

def _tomar(posse: Path) -> bool:
    try:
        fd = os.open(posse, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if not _posse_orfa(posse):
            return False
        print('   posse ORFA retomada (user morto): %s' % posse.name)
        try:
            posse.unlink()
            fd = os.open(posse, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (OSError, FileExistsError):
            return False
    with os.fdopen(fd, 'w') as fh:
        fh.write(str(os.getpid()))
    return True

def destilar(origem: Path, anel, fluxos: int, gemini_em_dez: int=0) -> tuple[int, int, int]:
    destino = FICHAS / (origem.stem + '.json')
    parcial = FICHAS / (origem.stem + '.parcial.json')
    if destino.exists():
        return (0, 0, 0)
    posse = FICHAS / (origem.stem + '.posse')
    if not _tomar(posse):
        return (0, 0, 0)
    texto = origem.read_text(encoding='utf-8', errors='replace')
    partes = da.lotes(texto)
    feitos: set[int] = set()
    fichas: list[dict] = []
    if parcial.exists():
        try:
            j = json.loads(parcial.read_text(encoding='utf-8'))
            feitos = set(j.get('lotes_feitos', []))
            fichas = j.get('fichas', [])
        except Exception:
            feitos, fichas = (set(), [])
    pendentes = [(i, p) for i, p in enumerate(partes) if i not in feitos]
    falhos = 0
    trava = threading.Lock()
    modelos = set()
    CAM_NEM = da.CAMADA_B
    ROTA_GEM = ('gemini', 'gemini', 'gemini-3.5-flash', da.URL_GEMINI)
    CAM_GEM = [ROTA_GEM] + list(CAM_NEM)
    CAM_NEM = list(CAM_NEM) + [ROTA_GEM]
    fatia = max(0, min(10, int(gemini_em_dez)))
    GRATIS = [r for r in da.CAMADA_B if not _e_paga(r)]
    PAGAS = [r for r in da.CAMADA_B if _e_paga(r)]

    def camada_do_lote(i: int):
        if fatia and CAM_GEM and (i % 10 < fatia):
            return CAM_GEM
        if len(GRATIS) < 2:
            return CAM_NEM
        k = i % len(GRATIS)
        return GRATIS[k:] + GRATIS[:k] + PAGAS + [ROTA_GEM]

    def processar(par):
        i, lote = par
        try:
            saida, modelo = da.chamar(anel, lote, camada_do_lote(i))
            return (i, saida, '', modelo)
        except Exception as erro:
            return (i, None, str(erro), '')
    with ThreadPoolExecutor(max_workers=fluxos) as pool:
        for n, (i, saida, erro, modelo) in enumerate(pool.map(processar, pendentes), 1):
            with trava:
                if modelo:
                    modelos.add(modelo)
                if saida is None:
                    falhos += 1
                else:
                    for f in saida:
                        if isinstance(f, dict):
                            f['fonte_video'] = origem.stem
                            fichas.append(f)
                    feitos.add(i)
                if n % 10 == 0 or n == len(pendentes):
                    da._gravar_atomico(parcial, {'lotes_feitos': sorted(feitos), 'de_um_total_de': len(partes), 'fichas': fichas})
    if not fichas:
        rotas = set()
        for m in modelos:
            rotas.update(m.split('+'))
        acordo = falhos == 0 and len(rotas) >= 2
        if acordo:
            da._gravar_atomico(destino, {'esquema': ESQUEMA, 'fonte': origem.name, 'lotes': len(partes), 'lotes_falhos': 0, 'corrida_morta': False, 'sem_saida_confirmado': True, 'rotas_que_concordaram': sorted(rotas), 'modelos': sorted(modelos), 'fichas': [], 'quando': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
            parcial.unlink(missing_ok=True)
            posse.unlink(missing_ok=True)
            return (0, 0, 1)
        FALHAS.mkdir(parents=True, exist_ok=True)
        da._gravar_atomico(FALHAS / (origem.stem + '.json'), {'esquema': ESQUEMA, 'fonte': origem.name, 'lotes': len(partes), 'lotes_falhos': falhos, 'corrida_morta': falhos == len(partes) and len(partes) > 0, 'modelos': sorted(modelos), 'fichas': [], 'quando': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
        parcial.unlink(missing_ok=True)
        posse.unlink(missing_ok=True)
        return (0, falhos, 1)
    da._gravar_atomico(destino, {'esquema': ESQUEMA, 'fonte': origem.name, 'lotes': len(partes), 'lotes_falhos': falhos, 'corrida_morta': falhos == len(partes) and len(partes) > 0, 'modelos': sorted(modelos), 'fichas': fichas})
    parcial.unlink(missing_ok=True)
    posse.unlink(missing_ok=True)
    return (len(fichas), falhos, 0)

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--fluxos', type=int, default=int(os.environ.get('WORKERS', '8')))
    ap.add_argument('--limite', type=int, default=0, help='destila no maximo N videos (0 = todos)')
    ap.add_argument('--videos-juntos', type=int, default=int(os.environ.get('APP_VIDEOS_JUNTOS', '1')), help='quantos VIDEOS destilar ao mesmo tempo (padrao 1)')
    ap.add_argument('--prefixo', default='', help="so' destila videos cujo nome comeca assim (ex: CANAL_C)")
    ap.add_argument('--reverso', action='store_true')
    ap.add_argument('--corpus', type=Path, default=None, help='pasta de entrada (padrao: corpus/)')
    ap.add_argument('--fichas', type=Path, default=None, help='pasta de saida (padrao: fichas/)')
    ap.add_argument('--gemini-em-dez', type=int, default=0, metavar='N', help='N de cada 10 lotes vao para o Gemini (0 = nenhum)')
    args = ap.parse_args()
    global CORPUS, FICHAS, FALHAS
    if args.corpus:
        CORPUS = args.corpus
    if args.fichas:
        FICHAS = args.fichas
    FALHAS = FICHAS / '_falhas'
    FICHAS.mkdir(parents=True, exist_ok=True)
    instrucao, esquema, origem = (INSTRUCAO, ESQUEMA, 'embutida')
    fora = CORPUS.parent / 'instrucao_ia.json'
    if fora.is_file():
        try:
            d = json.loads(fora.read_text(encoding='utf-8'))
            if d.get('instrucao') and d.get('esquema'):
                instrucao, esquema, origem = (d['instrucao'], d['esquema'], str(fora))
        except Exception as e:
            print('⚠️ %s ilegivel (%s) -- usando a embutida' % (fora, e))
    if not (instrucao or '').strip():
        raise SystemExit('⛔ sem INSTRUCAO: esperava %s' % fora)
    da.INSTRUCAO = instrucao
    globals()['ESQUEMA'] = esquema
    print('instrucao:', origem)
    anel = da.Anel()
    vivas = {k: len(v) for k, v in anel.chaves.items() if v}
    print('esquema:', ESQUEMA)
    print('camada : CAMADA_B (Nemotron nas tres rotas, SEM gemini)')
    print('chaves :', vivas or '⛔ NENHUMA -- nada a fazer')
    if not vivas:
        return 2
    fila = obras()
    if args.reverso:
        fila = list(reversed(fila))
    pendentes = [o for o in fila if not (FICHAS / (o.stem + '.json')).exists()]
    if args.prefixo:
        pendentes = [o for o in pendentes if o.stem.startswith(args.prefixo)]
    print('videos no corpus:', len(fila), '| SEM ficha:', len(pendentes))
    fila = pendentes[:args.limite] if args.limite else pendentes
    print('videos na fila:', len(fila), '\n')
    total_f = total_x = total_v = feitos = 0
    trava_saida = threading.Lock()

    def uma(par):
        n, origem = par
        alvo = FICHAS / (origem.stem + '.json')
        if alvo.exists():
            return (0, 0, 0)
        f, x, v = destilar(origem, anel, args.fluxos, args.gemini_em_dez)
        if f or x or v:
            with trava_saida:
                print('[%3d/%3d] %-58s %4d fichas | %d falhos' % (n, len(fila), '', f, x), flush=True)
        return (f, x, v)
    pares = list(enumerate(fila, 1))
    if args.videos_juntos > 1:
        with ThreadPoolExecutor(max_workers=args.videos_juntos) as pool:
            resultados = list(pool.map(uma, pares))
    else:
        resultados = [uma(par) for par in pares]
    for f, x, v in resultados:
        if f or x or v:
            feitos += 1
            total_f += f
            total_x += x
            total_v += v
    print('\nvideos destilados nesta corrida:', feitos)
    print('fichas:', total_f, '| lotes falhos:', total_x, '| videos sem saida:', total_v)
    print('saida:', FICHAS)
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
