#!/usr/bin/env python3
"""Valida sintaxe de um script Luau usando o compilador Lua 5.4.

Luau tem construcoes que o Lua 5.4 nao aceita. Este script traduz essas
construcoes para o equivalente em Lua padrao ANTES de validar, para que o
`luac -p` reporte apenas erros de sintaxe reais.

Traducoes aplicadas:
  x += y        -> x = x + y      (idem -= *= /= ..=)
  for k,v in t do -> for k,v in pairs(t) do   (iteracao generalizada do Luau)
  continue      -> removido (nao existe no Lua 5.4)
  ::= tipos     -> removidos (anotacoes de tipo do Luau)
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

COMPOUND = re.compile(r'([A-Za-z_][\w.\[\]"\']*)\s*(\+|-|\*|/|\.\.)=\s*')


def luau_to_lua(src: str) -> str:
    out = []
    for line in src.split('\n'):
        code = line
        # Operadores compostos: x += y  ->  x = x + y
        m = COMPOUND.search(code)
        while m:
            var, op = m.group(1), m.group(2)
            code = code[:m.start()] + f'{var} = {var} {op} ' + code[m.end():]
            m = COMPOUND.search(code, m.start() + len(var) + 4)

        # Iteracao generalizada: for a,b in expr do -> for a,b in pairs(expr) do
        gi = re.match(r'^(\s*)for\s+([\w,\s]+)\s+in\s+(.+?)\s+do\s*$', code)
        if gi:
            indent, names, expr = gi.groups()
            e = expr.strip()
            if not re.match(r'^(pairs|ipairs|next|.*:GetChildren\(\)|.*:GetDescendants\(\))\b', e):
                if not e.startswith(('pairs(', 'ipairs(')):
                    code = f'{indent}for {names} in pairs({e}) do'

        # continue nao existe no Lua 5.4
        if re.match(r'^\s*continue\s*$', code):
            code = re.sub(r'continue', '-- continue', code)

        out.append(code)
    return '\n'.join(out)


def validate(path: Path) -> int:
    src = path.read_text(encoding='utf-8')
    converted = luau_to_lua(src)

    with tempfile.NamedTemporaryFile('w', suffix='.lua', delete=False,
                                     encoding='utf-8') as tmp:
        tmp.write(converted)
        tmp_path = tmp.name

    proc = subprocess.run(['luac5.4', '-p', tmp_path],
                          capture_output=True, text=True)
    lines = src.split('\n')

    if proc.returncode == 0:
        print(f'OK  {path.name}: {len(lines)} linhas, '
              f'{len(src)/1024:.1f} KB — sintaxe valida')
        return 0

    err = proc.stderr.strip()
    print(f'ERRO  {path.name}')
    print(f'  {err}')
    m = re.search(r':(\d+):', err)
    if m:
        n = int(m.group(1))
        lo, hi = max(1, n - 3), min(len(lines), n + 3)
        print('  contexto:')
        for i in range(lo, hi + 1):
            mark = '>>' if i == n else '  '
            print(f'  {mark} {i:5}| {lines[i-1]}')
    Path(tmp_path).unlink(missing_ok=True)
    return 1


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('uso: validate_luau.py <arquivo.lua> [...]')
        sys.exit(2)
    rc = 0
    for arg in sys.argv[1:]:
        rc |= validate(Path(arg))
    sys.exit(rc)
